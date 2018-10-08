import torch
import torch.nn as nn
from torch.autograd import Variable

try:
    from log_uniform import LogUniformSampler
except ImportError:
    pass


class SampledSoftmax(nn.Module):
    def __init__(self, vocab_size, nsampled, hidden_size):
        super(SampledSoftmax, self).__init__()

        # Parameters
        self.vocab_size = vocab_size
        self.nsampled = nsampled

        try:
            self.sampler = LogUniformSampler(self.vocab_size)
        except NameError:
            raise ImportError("To use the Sampled Softmax module, "
                              "please install cython (pip install cython) "
                              "and then install the log_uniform package by "
                              "running \"python setup.py\" from the "
                              "onmt/modules/log_uniform directory.")
        self.params = nn.Linear(hidden_size, vocab_size)
        self.logsoftmax = nn.LogSoftmax(-1)

    def forward(self, inputs, labels):
        if self.training:
            # sample ids according to word distribution - Unique
            sample_values = self.sampler.sample(self.nsampled,
                                                labels.data.cpu().numpy())
            return self.sampled(inputs, labels, sample_values,
                                remove_accidental_match=True)
        else:
            return self.full(inputs, labels)

    def sampled(self, inputs, labels, sample_values,
                remove_accidental_match=False):
        assert(inputs.data.get_device() == labels.data.get_device())
        device_id = labels.data.get_device()

        batch_size, d = inputs.size()
        sample_ids, true_freq, sample_freq = sample_values

        sample_ids = Variable(torch.LongTensor(sample_ids)).cuda(device_id)
        true_freq = Variable(torch.FloatTensor(true_freq)).cuda(device_id)
        sample_freq = Variable(torch.FloatTensor(sample_freq)).cuda(device_id)

        # gather true labels - weights and frequencies
        true_weights = self.params.weight[labels, :]
        true_bias = self.params.bias[labels]

        # gather sample ids - weights and frequencies
        sample_weights = self.params.weight[sample_ids, :]
        sample_bias = self.params.bias[sample_ids]

        # calculate logits
        true_logits = torch.sum(torch.mul(inputs, true_weights), dim=1)\
            + true_bias
        sample_logits = torch.matmul(inputs, torch.t(sample_weights))\
            + sample_bias
        # remove true labels from sample set
        if remove_accidental_match:
            acc_hits = self.sampler.accidental_match(
                                labels.data.cpu().numpy(),
                                sample_ids.data.cpu().numpy())
            acc_hits = list(zip(*acc_hits))
            sample_logits[acc_hits] = -1e37

        # perform correction
        true_logits = true_logits.sub(torch.log(true_freq))
        sample_logits = sample_logits.sub(torch.log(sample_freq))

        # return logits and new_labels
        logits = torch.cat((torch.unsqueeze(true_logits, dim=1),
                           sample_logits), dim=1)
        new_targets = Variable(torch.zeros(batch_size).long()).cuda(device_id)
        return self.logsoftmax(logits), new_targets

    def full(self, inputs, labels=None):
        return self.logsoftmax(self.params(inputs)), labels
