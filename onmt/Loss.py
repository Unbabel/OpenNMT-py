import onmt
import onmt.Constants
import torch
import torch.nn as nn
from torch.autograd import Variable
import time
import sys
import math
import pdb

def NMTCriterion(vocabSize, opt):
    """
    Construct the standard NMT Criterion
    """
    weight = torch.ones(vocabSize)
    weight[onmt.Constants.PAD] = 0
    crit = nn.NLLLoss(weight, size_average=False)
    #crit = nn.NLLLoss(size_average=False, ignore_index=onmt.Constants.PAD)
    if opt.gpus:
        crit.cuda()
    return crit


def shardVariables(variables, batches, eval):
    """
    Split a dict of variables up into sharded dummy
    variables.
    """
    dummies = {}
    n_shards = ((list(variables.values())[0].size(0) - 1) // batches) + 1
    shards = [{} for _ in range(n_shards)]
    for k in variables:
        if isinstance(variables[k], Variable) and variables[k].requires_grad:
            dummies[k] = Variable(variables[k].data, requires_grad=(not eval),
                                  volatile=eval)
        else:
            dummies[k] = variables[k]
        #import pdb; pdb.set_trace()
        splits = torch.split(dummies[k], batches)
        for i, v in enumerate(splits):
            shards[i][k] = v
    return shards, dummies


def collectGrads(variables, dummy):
    """Given a set of variables, find the ones with gradients"""
    inputs = []
    grads = []
    for k in dummy:
        if isinstance(variables[k], Variable) and (dummy[k].grad is not None):
            inputs.append(variables[k])
            grads.append(dummy[k].grad.data)
    return inputs, grads


class Statistics:
    """
    Training loss function statistics.
    """
    def __init__(self, loss=0, reg=0, n_words=0, n_correct=0):
        self.loss = loss
        self.reg = reg
        self.n_words = n_words
        self.n_correct = n_correct
        self.n_src_words = 0
        self.start_time = time.time()

    def update(self, stat):
        self.loss += stat.loss
        self.reg += stat.reg
        self.n_words += stat.n_words
        self.n_correct += stat.n_correct

    def accuracy(self):
        return 100 * (self.n_correct / float(self.n_words))

    def ppl(self):
        return math.exp(min(self.loss / self.n_words, 100))

    def elapsed_time(self):
        return time.time() - self.start_time

    def output(self, epoch, batch, n_batches, start):
        t = self.elapsed_time()
        print(("Epoch %2d, %5d/%5d; acc: %6.2f; ppl: %6.2f; " +
               "loss: %6.2f; reg: %6.2f; "
               "%3.0f src tok/s; %3.0f tgt tok/s; %6.0f s elapsed") %
              (epoch, batch,  n_batches,
               self.accuracy(),
               self.ppl(),
               self.loss,
               self.reg,
               self.n_src_words / (t + 1e-5),
               self.n_words / (t + 1e-5),
               time.time() - start))
        sys.stdout.flush()

    def log(self, prefix, experiment, optim):
        t = self.elapsed_time()
        experiment.add_scalar_value(prefix + "_ppl", self.ppl())
        experiment.add_scalar_value(prefix + "_accuracy", self.accuracy())
        experiment.add_scalar_value(prefix + "_tgtper",  self.n_words / t)
        experiment.add_scalar_value(prefix + "_lr", optim.lr)


class MemoryEfficientLoss:
    """
    Class for best batchin the loss for NMT.
    """
    def __init__(self, opt, generator, crit,
                 copy_loss=False,
                 coverage_loss=False,
                 exhaustion_loss=False,
		 fertility_loss=False,
                 eval=False):
        """
        Args:
            generator (Function): ( any x rnn_size ) -> ( any x tgt_vocab )
            crit (Criterion): ( any x tgt_vocab )
            eval (bool): train or eval
        """
        self.generator = generator
        self.crit = crit
        self.eval = eval
        self.max_batches = opt.max_generator_batches
        self.copy_loss = copy_loss
        self.lambda_coverage = opt.lambda_coverage
        self.lambda_fertility = opt.lambda_fertility
        self.coverage_loss = coverage_loss
        self.exhaustion_loss = exhaustion_loss
        self.lambda_exhaust = opt.lambda_exhaust
        self.fertility_loss = fertility_loss
        self.mse = torch.nn.MSELoss()
        self.l1loss = torch.nn.L1Loss()

    def score(self, loss_t, reg_t, scores_t, targ_t):
        pred_t = scores_t.data.max(1)[1]
        non_padding = targ_t.ne(onmt.Constants.PAD).data
        num_correct_t = pred_t.eq(targ_t.data) \
                              .masked_select(non_padding) \
                              .sum()
        if reg_t is not None:
            return Statistics(loss_t.data[0], reg_t.data[0], non_padding.sum(),
                              num_correct_t)
        else:
            return Statistics(loss_t.data[0], 0, non_padding.sum(),
                              num_correct_t)

    def compute_std_loss(self, out_t, targ_t):
        scores_t = self.generator(out_t)
        loss_t = self.crit(scores_t, targ_t.view(-1))
        return loss_t, scores_t

    def compute_copy_loss(self, out_t, targ_t, attn_t, align_t):
        scores_t, c_attn_t = self.generator(out_t, attn_t)
        loss_t = self.crit(scores_t, c_attn_t, targ_t, align_t)
        return loss_t, scores_t

    def loss(self, batch, outputs, attns):
        """
        Args:
            batch (Batch): Data object
            outputs (FloatTensor): tgt_len x batch x rnn_size
            attns (dictionary): Dictionary of attention objects
        Returns:
            stats (Statistics): Statistics about loss
            inputs: list of variables with grads
            grads: list of grads corresponding to inputs
        """
        stats = Statistics()

        original = {"out_t": outputs,
                    "targ_t": batch.tgt[1:]}

        if self.coverage_loss:
            original["coverage_t"] = attns["coverage"]

        if self.copy_loss:
            original["attn_t"] = attns["copy"]
            original["align_t"] = batch.alignment[1:]
        if self.exhaustion_loss:
            original["upper_bounds_t"] = attns["upper_bounds"]
        if self.fertility_loss:
            original["true_fertility_vals_t"] = attns["true_fertility_vals"]
            original["predicted_fertility_vals_t"] = attns["predicted_fertility_vals"]

        shards, dummies = shardVariables(original, self.max_batches, self.eval)
        def bottle(v):
            return v.view(-1, v.size(2))
        for s in shards:
            if not self.copy_loss:
                loss_t, scores_t = self.compute_std_loss(bottle(s["out_t"]),
                                                         s["targ_t"])
            else:
                loss_t, scores_t = self.compute_copy_loss(
                    bottle(s["out_t"]), s["targ_t"],
                    bottle(s["attn_t"]), bottle(s["align_t"]))

            if self.coverage_loss:
                loss_t += self.lambda_coverage * torch.min(s["coverage_t"], s["attn_t"]).sum()

            if self.exhaustion_loss:
                #zero_mat = Variable(torch.Tensor([0]).repeat(upper_bounds.size(0), upper_bounds.size(1)).cuda())
                #loss_t += self.lambda_exhaust * self.mse(upper_bounds, zero_mat).sum()
                #loss_t += self.lambda_exhaust * s["upper_bounds_t"].sum()/(s["upper_bounds_t"].size(0)*s["upper_bounds_t"].size(1))
                #import pdb; pdb.set_trace()
                u_last_timestep = s["upper_bounds_t"][s["upper_bounds_t"].size(0)-1]  
                # Remove sink token
                indices = torch.arange(0,s["upper_bounds_t"].size(2)-1).long().cuda()
                #u_t = torch.index_select(u_last_timestep.data,1,indices)
                u_t = torch.index_select(u_last_timestep,1,Variable(indices))

                loss_t += self.lambda_exhaust * u_t.sum()
                # loss_t += self.lambda_exhaust * -1 * torch.pow(attns, 2).sum()

            #import pdb; pdb.set_trace()
            
            if self.fertility_loss and "predicted_fertility_vals_t" in s:
                #reg_t = self.lambda_fertility * self.mse(s["predicted_fertility_vals_t"][0], s["true_fertility_vals_t"][0])
                reg_t = self.lambda_fertility * self.l1loss(s["predicted_fertility_vals_t"][0], s["true_fertility_vals_t"][0])
                loss_t += reg_t
            else:
                reg_t = None

            stats.update(self.score(loss_t, reg_t, scores_t, s["targ_t"]))
            if not self.eval:
                loss_t.div(batch.batchSize).backward()

        # Return the gradients
        inputs, grads = collectGrads(original, dummies)
        return stats, inputs, grads
