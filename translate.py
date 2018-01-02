from __future__ import division
from builtins import bytes

import onmt
import onmt.Markdown
import onmt.IO
import torch
import argparse
import math
import codecs
import os
import evaluation
import pickle

parser = argparse.ArgumentParser(description='translate.py')
onmt.Markdown.add_md_help_argument(parser)

parser.add_argument('-model', required=True,
                    help='Path to model .pt file')
parser.add_argument('-src',   required=True,
                    help='Source sequence to decode (one line per sequence)')
parser.add_argument('-src_img_dir',   default="",
                    help='Source image directory')
parser.add_argument('-tgt',
                    help='True target sequence (optional)')
parser.add_argument('-output', default='pred.txt',
                    help="""Path to output the predictions (each line will
                    be the decoded sequence""")
parser.add_argument('-beam_size',  type=int, default=15,
                    help='Beam size')
parser.add_argument('-batch_size', type=int, default=1,
                    help='Batch size')
parser.add_argument('-max_sent_length', type=int, default=100,
                    help='Maximum sentence length.')
parser.add_argument('-replace_unk', action="store_true",
                    help="""Replace the generated UNK tokens with the source
                    token that had highest attention weight. If phrase_table
                    is provided, it will lookup the identified source token and
                    give the corresponding target token. If it is not provided
                    (or the identified source token does not exist in the
                    table) then it will copy the source token""")
# parser.add_argument('-phrase_table',
#                     help="""Path to source-target dictionary to replace UNK
#                     tokens. See README.md for the format of this file.""")
parser.add_argument('-guided_fertility', type=str, default=None,
                    help="""Get fertility values from external aligner, specify alignment file""")
parser.add_argument('-attn_transform', type=str, default=None,
                    choices=['softmax', 'constrained_softmax','sparsemax',
                             'constrained_sparsemax'],
                    help="""The attention transform to use (None means the one stored in the model.""")
parser.add_argument('-fertility', type=float, default=None,
                    help="""Constant fertility value for each word in the source (None means the one stored in the model.""")

parser.add_argument('-verbose', action="store_true",
                    help='Print scores and predictions for each sentence')
parser.add_argument('-attn_debug', action="store_true",
                    help='Print best attn for each word')

parser.add_argument('-dump_beam', type=str, default="",
                    help='File to dump beam information to.')

parser.add_argument('-heatmap', action="store_true",
                    help='Store attention heatmaps.')

parser.add_argument('-n_best', type=int, default=1,
                    help="""If verbose is set, will output the n_best
                    decoded sentences""")

parser.add_argument('-gpu', type=int, default=-1,
                    help="Device to run on")

def reportScore(name, scoreTotal, wordsTotal):
    print("%s AVG SCORE: %.4f, %s PPL: %.4f" % (
        name, scoreTotal / wordsTotal,
        name, math.exp(-scoreTotal/wordsTotal)))


def addone(f):
    for line in f:
        yield line
    yield None


def main():
    opt = parser.parse_args()
    opt.cuda = opt.gpu > -1
    if opt.cuda:
        torch.cuda.set_device(opt.gpu)

    translator = onmt.Translator(opt)

    outF = codecs.open(opt.output, 'w', 'utf-8')

    predScoreTotal, predWordsTotal, goldScoreTotal, goldWordsTotal = 0, 0, 0, 0

    srcBatch, tgtBatch = [], []
 
    attn_matrices = []   

    count = 0

    tgtF = codecs.open(opt.tgt, 'r', 'utf-8') if opt.tgt else None

    if opt.dump_beam != "":
        import json
        translator.initBeamAccum()

    for k, line in enumerate(addone(codecs.open(opt.src, 'r', 'utf-8'))):
        if line is not None:
            srcTokens = line.split()
            srcBatch += [srcTokens]
            if tgtF:
                tgtTokens = tgtF.readline().split() if tgtF else None
                tgtBatch += [tgtTokens]

            if len(srcBatch) < opt.batch_size:
                continue
        else:
            # at the end of file, check last batch
            if len(srcBatch) == 0:
                break

        predBatch, predScore, goldScore, attn, src \
            = translator.translate(srcBatch, tgtBatch)
        #attn_matrices.append(attn)
        # Store attention heatmaps
        if opt.heatmap:
            evaluation.plot_heatmap(opt.model, attn, k, srcBatch[0], predBatch[0][0])

        predScoreTotal += sum(score[0] for score in predScore)
        predWordsTotal += sum(len(x[0]) for x in predBatch)
        if tgtF is not None:
            goldScoreTotal += sum(goldScore)
            goldWordsTotal += sum(len(x) for x in tgtBatch)

        for b in range(len(predBatch)):
            count += 1
            outF.write(" ".join([i for i in predBatch[b][0]]) + '\n')
            outF.flush()

            if opt.verbose:
                srcSent = ' '.join(srcBatch[b])
                if translator.tgt_dict.lower:
                    srcSent = srcSent.lower()
                os.write(1, bytes('SENT %d: %s\n' % (count, srcSent), 'UTF-8'))
                os.write(1, bytes('PRED %d: %s\n' %
                                  (count, " ".join(predBatch[b][0])), 'UTF-8'))
                print("PRED SCORE: %.4f" % predScore[b][0])

                if tgtF is not None:
                    tgtSent = ' '.join(tgtBatch[b])
                    if translator.tgt_dict.lower:
                        tgtSent = tgtSent.lower()
                    os.write(1, bytes('GOLD %d: %s\n' %
                             (count, tgtSent), 'UTF-8'))
                    print("GOLD SCORE: %.4f" % goldScore[b])

                if opt.n_best > 1:
                    print('\nBEST HYP:')
                    for n in range(opt.n_best):
                        os.write(1, bytes("[%.4f] %s\n" % (predScore[b][n],
                                 " ".join(predBatch[b][n])),
                            'UTF-8'))

                if opt.attn_debug:
                    print('')
                    for i, w in enumerate(predBatch[b][0]):
                        print(w)
                        _, ids = attn[b][0][i].sort(0, descending=True)
                        for j in ids[:5].tolist():
                            print("\t%s\t%d\t%3f" % (srcTokens[j], j,
                                                     attn[b][0][i][j]))

        srcBatch, tgtBatch = [], []
    #pickle.dump(attn_matrices, open('attn_matrices_fert5_sink.out', 'wb'))
    reportScore('PRED', predScoreTotal, predWordsTotal)
    if tgtF:
        reportScore('GOLD', goldScoreTotal, goldWordsTotal)

    if tgtF:
        tgtF.close()

    if opt.dump_beam:
        json.dump(translator.beam_accum,
                  codecs.open(opt.dump_beam, 'w', 'utf-8'))


if __name__ == "__main__":
    main()
