from __future__ import division
"""
This is the loadable seq2seq trainer library that is
in charge of training details, loss compute, and statistics.
See train.py for a use case of this library.

Note!!! To make this a general library, we implement *only*
mechanism things here(i.e. what to do), and leave the strategy
things to users(i.e. how to do it). Also see train.py(one of the
users of this library) for the strategy things we do.
"""
import time
import sys
import math

import torch
import torch.nn as nn

import onmt
import onmt.io
import onmt.modules


class Statistics(object):
    """
    Accumulator for loss statistics.
    Currently calculates:

    * accuracy
    * perplexity
    * elapsed time
    """
    def __init__(self, loss=0, n_words=0, n_correct=0):
        self.loss = loss
        self.n_words = n_words
        self.n_correct = n_correct
        self.n_src_words = 0
        self.n_mt_words = 0
        self.start_time = time.time()

    def update(self, stat):
        self.loss += stat.loss
        self.n_words += stat.n_words
        self.n_correct += stat.n_correct

    def accuracy(self):
        return 100 * (self.n_correct / self.n_words)

    def xent(self):
        return self.loss / self.n_words

    def ppl(self):
        return math.exp(min(self.loss / self.n_words, 100))

    def elapsed_time(self):
        return time.time() - self.start_time

    def output(self, epoch, batch, n_batches, start):
        """Write out statistics to stdout.

        Args:
           epoch (int): current epoch
           batch (int): current batch
           n_batch (int): total batches
           start (int): start time of epoch.
        """
        t = self.elapsed_time()
        print(("Epoch %2d, %5d/%5d; acc: %6.2f; ppl: %6.2f; xent: %6.2f; " +
               "%3.0f src tok/s; %3.0f tgt tok/s; %6.0f s elapsed") %
              (epoch, batch,  n_batches,
               self.accuracy(),
               self.ppl(),
               self.xent(),
               self.n_src_words / (t + 1e-5),
               self.n_words / (t + 1e-5),
               time.time() - start))
        sys.stdout.flush()

    def log(self, prefix, experiment, lr):
        t = self.elapsed_time()
        experiment.add_scalar_value(prefix + "_ppl", self.ppl())
        experiment.add_scalar_value(prefix + "_accuracy", self.accuracy())
        experiment.add_scalar_value(prefix + "_tgtper",  self.n_words / t)
        experiment.add_scalar_value(prefix + "_lr", lr)

    def log_tensorboard(self, prefix, writer, lr, step):
        t = self.elapsed_time()
        writer.add_scalar(prefix + "/xent", self.xent(), step)
        writer.add_scalar(prefix + "/ppl", self.ppl(), step)
        writer.add_scalar(prefix + "/accuracy", self.accuracy(), step)
        writer.add_scalar(prefix + "/tgtper",  self.n_words / t, step)
        writer.add_scalar(prefix + "/lr", lr, step)


class Trainer(object):
    """
    Class that controls the training process.

    Args:
            model(:py:class:`onmt.Model.NMTModel`): translation model to train

            train_loss(:obj:`onmt.Loss.LossComputeBase`):
               training loss computation
            valid_loss(:obj:`onmt.Loss.LossComputeBase`):
               training loss computation
            optim(:obj:`onmt.Optim.Optim`):
               the optimizer responsible for update
            trunc_size(int): length of truncated back propagation through time
            shard_size(int): compute loss in shards of this size for efficiency
            data_type(string): type of the source input: [text|img|audio]
            norm_method(string): normalization methods: [sents|tokens]
            grad_accum_count(int): accumulate gradients this many times.
    """

    def __init__(self, model, train_loss, valid_loss, optim,
                 trunc_size=0, shard_size=32, data_type='text',
                 norm_method="sents", grad_accum_count=1,
                 elmo=False):
        # Basic attributes.
        self.model = model
        self.train_loss = train_loss
        self.valid_loss = valid_loss
        self.optim = optim
        self.trunc_size = trunc_size
        self.shard_size = shard_size
        self.data_type = data_type
        self.norm_method = norm_method
        self.grad_accum_count = grad_accum_count
        self.progress_step = 0
        self.elmo = elmo

        assert(grad_accum_count > 0)
        if grad_accum_count > 1:
            assert(self.trunc_size == 0), \
                """To enable accumulated gradients,
                   you must disable target sequence truncating."""

        # Set model in training mode.
        self.model.train()

    def train(self, train_iter, epoch, report_func=None, **kwargs):
        """ Train next epoch.
        Args:
            train_iter: training data iterator
            epoch(int): the epoch number
            report_func(fn): function for logging

        Returns:
            stats (:obj:`onmt.Statistics`): epoch loss statistics
        """
        total_stats = Statistics()
        report_stats = Statistics()
        idx = 0
        true_batchs = []
        accum = 0
        normalization = 0
        try:
            add_on = 0
            if len(train_iter) % self.grad_accum_count > 0:
                add_on += 1
            num_batches = len(train_iter) / self.grad_accum_count + add_on
        except NotImplementedError:
            # Dynamic batching
            num_batches = -1

        for i, batch in enumerate(train_iter):
            cur_dataset = train_iter.get_cur_dataset()
            self.train_loss.cur_dataset = cur_dataset

            true_batchs.append(batch)
            accum += 1
            if self.norm_method == "tokens":
                num_tokens = batch.tgt[1:].data.view(-1) \
                    .ne(self.train_loss.padding_idx).sum()
                normalization += num_tokens
            else:
                normalization += batch.batch_size

            if accum == self.grad_accum_count:
                self._gradient_accumulation(
                        true_batchs, total_stats,
                        report_stats, normalization)

                if report_func is not None:
                    report_stats = report_func(
                            epoch, idx, num_batches,
                            self.progress_step,
                            total_stats.start_time, self.optim.lr,
                            report_stats)
                    self.progress_step += 1

                true_batchs = []
                accum = 0
                normalization = 0
                idx += 1

        if len(true_batchs) > 0:
            self._gradient_accumulation(
                    true_batchs, total_stats,
                    report_stats, normalization)
            true_batchs = []

        return total_stats

    def validate(self, valid_iter):
        """ Validate model.
            valid_iter: validate data iterator
        Returns:
            :obj:`onmt.Statistics`: validation loss statistics
        """
        # Set model in validating mode.
        self.model.eval()

        stats = Statistics()

        for batch in valid_iter:
            cur_dataset = valid_iter.get_cur_dataset()
            self.valid_loss.cur_dataset = cur_dataset

            src = onmt.io.make_features(batch, 'src', self.data_type)
            if self.data_type == 'text':
                _, src_lengths = batch.src
            else:
                src_lengths = None

            if self.elmo:
                char_src = onmt.io.make_features(batch, 'char_src')
                # (target_size, batch_size, max_char_src, n_feat)
                char_src = char_src.permute(1, 0, 3, 2).contiguous()
            else:
                char_src = None

            tgt = onmt.io.make_features(batch, 'tgt')

            # F-prop through the model.
            outputs, attns, _ = self.model(src, tgt, src_lengths,
                                           char_src=char_src)

            # Compute loss.
            batch_stats = self.valid_loss.monolithic_compute_loss(
                    batch, outputs, attns)

            # Update statistics.
            stats.update(batch_stats)

        # Set model back to training mode.
        self.model.train()

        return stats

    def epoch_step(self, ppl, epoch):
        return self.optim.update_learning_rate(ppl, epoch)

    def drop_checkpoint(self, opt, epoch, fields, valid_stats):
        """ Save a resumable checkpoint.

        Args:
            opt (dict): option object
            epoch (int): epoch number
            fields (dict): fields and vocabulary
            valid_stats : statistics of last validation run
        """
        checkpoint = self._generate_checkpoint(opt, epoch, fields)
        torch.save(checkpoint,
                   '%s_acc_%.2f_ppl_%.2f_e%d.pt'
                   % (opt.save_model, valid_stats.accuracy(),
                      valid_stats.ppl(), epoch))

    def _generate_checkpoint(self, opt, epoch, fields):
        real_model = (self.model.module
                      if isinstance(self.model, nn.DataParallel)
                      else self.model)
        real_generator = (real_model.generator.module
                          if isinstance(real_model.generator, nn.DataParallel)
                          else real_model.generator)

        model_state_dict = real_model.state_dict()
        model_state_dict = {k: v for k, v in model_state_dict.items()
                            if 'generator' not in k}
        generator_state_dict = real_generator.state_dict()
        checkpoint = {
            'model': model_state_dict,
            'generator': generator_state_dict,
            'vocab': onmt.io.save_fields_to_vocab(fields),
            'opt': opt,
            'epoch': epoch,
            'optim': self.optim,
        }
        return checkpoint

    def drop_best_earlystopping(self, opt, epoch, fields):
        """ Save a resumable checkpoint (the best model so far).

        Args:
            opt (dict): option object
            epoch (int): epoch number
            fields (dict): fields and vocabulary
        """
        checkpoint = self._generate_checkpoint(opt, epoch, fields)
        torch.save(checkpoint,
                   '{}_best.pt'.format(opt.save_model))

    def _gradient_accumulation(self, true_batchs, total_stats,
                               report_stats, normalization):
        if self.grad_accum_count > 1:
            self.model.zero_grad()

        for batch in true_batchs:
            target_size = batch.tgt.size(0)
            # Truncated BPTT
            if self.trunc_size:
                trunc_size = self.trunc_size
            else:
                trunc_size = target_size

            dec_state = None
            src = onmt.io.make_features(batch, 'src', self.data_type)
            if self.data_type == 'text':
                _, src_lengths = batch.src
                report_stats.n_src_words += src_lengths.sum()
            else:
                src_lengths = None

            if self.elmo:
                char_src = onmt.io.make_features(batch, 'char_src')
                # (target_size, batch_size, max_char_src, n_feat)
                char_src = char_src.permute(1, 0, 3, 2).contiguous()
            else:
                char_src = None

            tgt_outer = onmt.io.make_features(batch, 'tgt')

            for j in range(0, target_size-1, trunc_size):
                # 1. Create truncated target.
                tgt = tgt_outer[j: j + trunc_size]

                # 2. F-prop all but generator.
                if self.grad_accum_count == 1:
                    self.model.zero_grad()
                outputs, attns, dec_state = \
                    self.model(src, tgt, src_lengths, dec_state,
                               char_src=char_src)

                # 3. Compute loss in shards for memory efficiency.
                batch_stats = self.train_loss.sharded_compute_loss(
                        batch, outputs, attns, j,
                        trunc_size, self.shard_size, normalization)

                # 4. Update the parameters and statistics.
                if self.grad_accum_count == 1:
                    self.optim.step()
                total_stats.update(batch_stats)
                report_stats.update(batch_stats)

                # If truncated, don't backprop fully.
                if dec_state is not None:
                    dec_state.detach()

        if self.grad_accum_count > 1:
            self.optim.step()


class EarlyStoppingTrainer(Trainer):
    """
    Trainer Class with additional early stopping mechanism after N batches.

    Args:
            model(:py:class:`onmt.Model.NMTModel`): translation model to train

            train_loss(:obj:`onmt.Loss.LossComputeBase`):
               training loss computation
            valid_loss(:obj:`onmt.Loss.LossComputeBase`):
               training loss computation
            optim(:obj:`onmt.Optim.Optim`):
               the optimizer responsible for update
            tolerance(int): max number of validation steps without improving
            epochs(int): number of epochs
            model_opt(list): options
            fields(list): vocab fields
            trunc_size(int): length of truncated back propagation through time
            shard_size(int): compute loss in shards of this size for efficiency
            data_type(string): type of the source input: [text|img|audio]
            norm_method(string): normalization methods: [sents|tokens]
            grad_accum_count(int): accumulate gradients this many times.
    """

    def __init__(self, model, train_loss, valid_loss, optim, tolerance, epochs,
                 model_opt, fields,
                 trunc_size=0, shard_size=32, data_type='text',
                 norm_method="sents", grad_accum_count=1,
                 elmo=False,
                 start_val_after_batches=1000):

        # Basic attributes. Trainer holds every generic information.
        super(EarlyStoppingTrainer,
              self).__init__(model, train_loss, valid_loss, optim,
                             trunc_size=trunc_size, shard_size=shard_size,
                             data_type=data_type, norm_method=norm_method,
                             grad_accum_count=grad_accum_count,
                             elmo=elmo)

        self.model_opt = model_opt
        self.fields = fields
        self.start_val_after_batches = start_val_after_batches
        self.current_batches_processed = 0
        if epochs is None:
            epochs = 10
        self.patience = EarlyStopping(tolerance=tolerance,
                                      epochs=epochs, trainer=self)

    def train(self, train_iter, epoch, valid_iter=None,
              report_func=None, **kwargs):
        """ Train next epoch.
        Args:
            train_iter: training data iterator
            epoch(int): the epoch number
            valid_iter(fn): validation data builder, needs to be callable
            report_func(fn): function for logging

        Returns:
            stats (:obj:`onmt.Statistics`): epoch loss statistics
        """
        if valid_iter is None:
            import warnings
            warnings.warn("""Validation iterator not provided.
                          Not performing early stopping.""")

        total_stats = Statistics()
        report_stats = Statistics()
        idx = 0
        true_batchs = []
        accum = 0
        normalization = 0
        try:
            add_on = 0
            if len(train_iter) % self.grad_accum_count > 0:
                add_on += 1
            num_batches = len(train_iter) / self.grad_accum_count + add_on
        except NotImplementedError:
            # Dynamic batching
            num_batches = -1

        valid_stats_epoch = []

        for i, batch in enumerate(train_iter):
            # Increment number of batches processed
            self.current_batches_processed += 1

            cur_dataset = train_iter.get_cur_dataset()
            self.train_loss.cur_dataset = cur_dataset

            true_batchs.append(batch)
            accum += 1
            if self.norm_method == "tokens":
                num_tokens = batch.tgt[1:].data.view(-1) \
                    .ne(self.train_loss.padding_idx).sum()
                normalization += num_tokens
            else:
                normalization += batch.batch_size

            if accum == self.grad_accum_count:
                self._gradient_accumulation(
                        true_batchs, total_stats,
                        report_stats, normalization)

                if report_func is not None:
                    report_stats = report_func(
                            epoch, idx, num_batches,
                            self.progress_step,
                            total_stats.start_time, self.optim.lr,
                            report_stats)
                    self.progress_step += 1

                true_batchs = []
                accum = 0
                normalization = 0
                idx += 1
            # Perform validation when the number of batches processed reaches
            #  the number of batches to validate
            if self.current_batches_processed == self.start_val_after_batches \
                    and valid_iter is not None:
                # Run validation and report stats
                self.current_batches_processed = 0
                valid_iter_lazy = valid_iter()
                valid_stats = self.validate(valid_iter_lazy)
                print('Validation perplexity: %g' % valid_stats.ppl())
                print('Validation accuracy: %g' % valid_stats.accuracy())
                valid_stats_epoch.append(valid_stats)
                # Run patience mechanism
                self.patience(valid_stats, epoch, self.model_opt, self.fields)
                # If the patience has reached the limit, stop training
                if self.patience.has_stopped():
                    break
                # Instead of saving every checkpoint,
                # save only the improving checkpoints! Save space.
                if self.patience.is_improving():
                    self.drop_checkpoint(self.model_opt, epoch,
                                         self.fields, valid_stats)

        if len(true_batchs) > 0:
            self._gradient_accumulation(
                    true_batchs, total_stats,
                    report_stats, normalization)
            true_batchs = []

        return total_stats, valid_stats_epoch if valid_iter is not None \
            else None, self.patience


class EarlyStopping(object):
    """
        Callable class to keep track of early stopping.

        Args:
                tolerance(int): max number of steps without improving
                epochs(int): number of epochs
                trainer(:py:class:`onmt.Trainer`): trainer
                scorer(fn): score models to determine the best one
        """
    from enum import Enum

    class PatienceEnum(Enum):
        IMPROVING = 0
        DECREASING = 1
        STOPPED = 2

    def __init__(self, tolerance, epochs, trainer, scorer=lambda x: x.ppl()):

        self.epochs = epochs
        self.tolerance = tolerance
        self.current_tolerance = self.tolerance
        self.best_score = float("inf")
        self.early_stopping_scorer = scorer
        self.trainer = trainer
        self.status = EarlyStopping.PatienceEnum.IMPROVING

    def __call__(self, valid_stats, epoch, model_opt, fields):
        if self.early_stopping_scorer(valid_stats) < self.best_score:
            print("Model is improving: {:g} --> {:g}."
                  .format(self.best_score,
                          self.early_stopping_scorer(valid_stats)))

            self.trainer.drop_best_earlystopping(model_opt, epoch, fields)
            self.current_tolerance = self.tolerance
            self.best_score = self.early_stopping_scorer(valid_stats)
            self.status = EarlyStopping.PatienceEnum.IMPROVING
        else:
            if self.early_stopping_scorer(valid_stats) > self.best_score:
                self.current_tolerance -= 1
                print("Decreasing patience: {}/{}"
                      .format(self.current_tolerance,
                              self.tolerance))
                if self.current_tolerance == 0:
                    print("Training finished. Early Stop! Best validation {:g}"
                          .format(self.best_score))

            self.status = EarlyStopping.PatienceEnum.DECREASING \
                if self.current_tolerance > 0 \
                else EarlyStopping.PatienceEnum.STOPPED

    def is_improving(self):
        return self.status == EarlyStopping.PatienceEnum.IMPROVING

    def has_stopped(self):
        return self.status == EarlyStopping.PatienceEnum.STOPPED


class LanguageModelTrainer(EarlyStoppingTrainer):

    def __init__(self, model, train_loss, valid_loss, optim,
                 tolerance, epochs, model_opt, fields,
                 trunc_size=0, shard_size=32, data_type='text',
                 norm_method="sents", grad_accum_count=1,
                 start_val_after_batches=1000):

        super(LanguageModelTrainer, self).__init__(
            model, train_loss,
            valid_loss, optim,
            tolerance, epochs, model_opt, fields,
            trunc_size, shard_size,
            data_type,
            norm_method,
            grad_accum_count,
            start_val_after_batches)

    def validate(self, valid_iter):
        """ Validate model.
            valid_iter: validate data iterator
        Returns:
            :obj:`onmt.Statistics`: validation loss statistics
        """
        # Set model in validating mode.
        self.model.eval()

        stats = Statistics()

        attns = None

        for batch in valid_iter:
            cur_dataset = valid_iter.get_cur_dataset()
            self.valid_loss.cur_dataset = cur_dataset

            init_hidden = self.model.init_rnn_state(batch.batch_size)

            if self.model.char_convs:
                tgt_input = onmt.io.make_features(batch, 'char_tgt')
                # (target_size, batch_size, max_char_tgt, n_feat)
                tgt_input = tgt_input.permute(1, 0, 3, 2).contiguous()
            else:
                tgt_input = onmt.io.make_features(batch, 'tgt')

            # F-prop through the model.
            outputs, _ = self.model(tgt_input, init_hidden)

            # Remove EOS/BOS output
            outputs = outputs[:, :-1, :, :, :].contiguous()

            # Compute loss.
            batch_stats = self.valid_loss.monolithic_compute_loss(
                    batch, outputs, attns)
            # remove attns for lm

            # Update statistics.
            stats.update(batch_stats)

        # Set model back to training mode.
        self.model.train()

        return stats

    def _gradient_accumulation(self, true_batchs, total_stats,
                               report_stats, normalization):
        if self.grad_accum_count > 1:
            self.model.zero_grad()

        for batch in true_batchs:
            target_size = batch.tgt.size(0)
            # Truncated BPTT
            if self.trunc_size:
                trunc_size = self.trunc_size
            else:
                trunc_size = target_size

            init_hidden = self.model.init_rnn_state(batch.batch_size)
            attns = None

            if self.model.char_convs:
                tgt_input = onmt.io.make_features(batch, 'char_tgt')
                # (target_size, batch_size, max_char_tgt, n_feat)
                tgt_input = tgt_input.permute(1, 0, 3, 2).contiguous()
            else:
                tgt_input = onmt.io.make_features(batch, 'tgt')

            for j in range(0, target_size-1, trunc_size):

                # 1. F-prop all but generator.
                if self.grad_accum_count == 1:
                    self.model.zero_grad()

                outputs, _ = self.model(tgt_input, init_hidden)

                # Remove EOS/BOS output
                outputs = outputs[:, :-1, :, :, :].contiguous()

                # 2. Compute loss in shards for memory efficiency.
                batch_stats = self.train_loss.sharded_compute_loss(
                        batch, outputs, attns, j,
                        trunc_size, self.shard_size, normalization)

                # 3. Update the parameters and statistics.
                if self.grad_accum_count == 1:
                    self.optim.step()
                total_stats.update(batch_stats)
                report_stats.update(batch_stats)

                # If truncated, don't backprop fully.
                # if hidden_state is not None:
                #     hidden_state.detach()
                #     hidden_state = self.model.init_rnn_state(
                # batch.tgt.size(1))

        if self.grad_accum_count > 1:
            self.optim.step()


class APETrainer(EarlyStoppingTrainer):

    def __init__(self, model, train_loss, valid_loss, optim,
                 tolerance, epochs, model_opt, fields,
                 trunc_size=0, shard_size=32, data_type='text',
                 norm_method="sents", grad_accum_count=1, elmo=False,
                 start_val_after_batches=1000):

        super(APETrainer, self).__init__(
           model, train_loss, valid_loss, optim, tolerance, epochs,
           model_opt, fields,
           trunc_size, shard_size, data_type,
           norm_method, grad_accum_count,
           elmo,
           start_val_after_batches)

    def validate(self, valid_iter):
        """ Validate model.
            valid_iter: validate data iterator
        Returns:
            :obj:`onmt.Statistics`: validation loss statistics
        """
        # Set model in validating mode.
        self.model.eval()

        stats = Statistics()

        for batch in valid_iter:
            cur_dataset = valid_iter.get_cur_dataset()
            self.valid_loss.cur_dataset = cur_dataset

            src = onmt.io.make_features(batch, 'src', self.data_type)
            if self.data_type == 'text':
                _, src_lengths = batch.src
            else:
                src_lengths = None

            if self.elmo:
                char_src = onmt.io.make_features(batch, 'char_src')
                # (target_size, batch_size, max_char_src, n_feat)
                char_src = char_src.permute(1, 0, 3, 2).contiguous()

                char_mt = onmt.io.make_features(batch, 'char_mt')
                # (target_size, batch_size, max_char_mt, n_feat)
                char_mt = char_mt.permute(1, 0, 3, 2).contiguous()
            else:
                char_src = None
                char_mt = None

            mt = onmt.io.make_features(batch, 'mt', self.data_type)
            if self.data_type == 'text':
                _, mt_lengths = batch.mt
            else:
                mt_lengths = None

            tgt = onmt.io.make_features(batch, 'tgt')

            # F-prop through the model.
            outputs, attns, _ = self.model(src, mt, tgt, src_lengths,
                                           mt_lengths,
                                           char_src=char_src,
                                           char_mt=char_mt)

            # Compute loss.
            batch_stats = self.valid_loss.monolithic_compute_loss(
                    batch, outputs, attns)

            # Update statistics.
            stats.update(batch_stats)

        # Set model back to training mode.
        self.model.train()

        return stats

    def _gradient_accumulation(self, true_batchs, total_stats,
                               report_stats, normalization):
        if self.grad_accum_count > 1:
            self.model.zero_grad()

        for batch in true_batchs:
            target_size = batch.tgt.size(0)
            # Truncated BPTT
            if self.trunc_size:
                trunc_size = self.trunc_size
            else:
                trunc_size = target_size

            dec_state = None
            src = onmt.io.make_features(batch, 'src', self.data_type)
            if self.data_type == 'text':
                _, src_lengths = batch.src
                report_stats.n_src_words += src_lengths.sum()
            else:
                src_lengths = None

            if self.elmo:
                char_src = onmt.io.make_features(batch, 'char_src')
                # (target_size, batch_size, max_char_src, n_feat)
                char_src = char_src.permute(1, 0, 3, 2).contiguous()

                char_mt = onmt.io.make_features(batch, 'char_mt')
                # (target_size, batch_size, max_char_mt, n_feat)
                char_mt = char_mt.permute(1, 0, 3, 2).contiguous()
            else:
                char_src = None
                char_mt = None

            mt = onmt.io.make_features(batch, 'mt', self.data_type)
            if self.data_type == 'text':
                _, mt_lengths = batch.mt
                report_stats.n_mt_words += mt_lengths.sum()
            else:
                mt_lengths = None

            tgt_outer = onmt.io.make_features(batch, 'tgt')

            for j in range(0, target_size-1, trunc_size):
                # 1. Create truncated target.
                tgt = tgt_outer[j: j + trunc_size]

                # 2. F-prop all but generator.
                if self.grad_accum_count == 1:
                    self.model.zero_grad()

                outputs, attns, dec_state = \
                    self.model(src, mt, tgt, src_lengths, mt_lengths,
                               dec_state,
                               char_src=char_src,
                               char_mt=char_mt)

                # 3. Compute loss in shards for memory efficiency.
                batch_stats = self.train_loss.sharded_compute_loss(
                        batch, outputs, attns, j,
                        trunc_size, self.shard_size, normalization)

                # 4. Update the parameters and statistics.
                if self.grad_accum_count == 1:
                    self.optim.step()
                total_stats.update(batch_stats)
                report_stats.update(batch_stats)

                # If truncated, don't backprop fully.
                if dec_state is not None:
                    dec_state.detach()

        if self.grad_accum_count > 1:
            self.optim.step()
