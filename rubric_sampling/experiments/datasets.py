from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import os
import sys
import random
import cPickle
import torch
import numpy as np
from tqdm import tqdm
from collections import defaultdict
from torch.utils.data.dataset import Dataset

from .utils import (
    ZIPF_CLASS,
    PAD_TOKEN,
    UNK_TOKEN,
    EOS_TOKEN,
    SOS_TOKEN,
    OrderedCounter,
    flatten_ast,
    removeColors,
)
from rubrics_utils.load_params import get_codeorg_data_root


def load_dataset(dataset, problem_id, split, vocab=None, max_seq_len=50, min_occ=3):
    r"""Helper function to load the proper dataset. Choose from
        
        - human annotations for a small set of programs
        - a large pool of unlabeled program strings
        - synthetic programs with feedback generated by a model

    @param dataset: string
                    unlabeled|annotated|synthetic
    
    Rest of the variables are inputs to CodeOrgDataset
    """
    assert dataset in ['unlabeled', 'annotated', 'synthetic']
    data_dir = get_codeorg_data_root(problem_id, dataset)
    
    return CodeOrgDataset(  data_dir, problem_id, split, vocab=vocab, 
                            max_seq_len=max_seq_len, min_occ=min_occ)


class CodeOrgDataset(Dataset):
    r"""Dataset for training a model on a (possibly unlabeled) dataset.

    @param dataset: string
                    unlabeled|annotated|synthetic
    @param problem_id: integer [1-8]
                       id of the problem in curricula
    @param split: string
                  which division of data to use
                  train|val|test|all
    @param vocab: ?object [default: None]
                  words that the model is aware o
    @param max_seq_len: integer [default: 50]
                        maximum number of tokens in a sequence.
    @param min_occ: integer [default: 3]
                    minimum number of times for a token to
                    be included in the vocabulary.
    """
    def __init__(self, dataset, problem_id, split, vocab=None, max_seq_len=50, min_occ=3):
        super(CodeOrgDataset, self).__init__()

        self.problem_id = problem_id
        self.split = split
        self.vocab = vocab
        self.max_seq_len = max_seq_len
        self.min_occ = min_occ

        self.data_dir = get_codeorg_data_root(problem_id, dataset)

        with open(os.path.join(self.data_dir, '%s.pickle' % self.split)) as fp:
            raw_data = cPickle.load(fp)
            assert 'programs' in raw_data
            self.raw_programs = raw_data['programs']
            if 'labels' in raw_data:
                self.raw_labels = raw_data['labels']
            else:
                self.raw_labels = None

        count_map = self.build_count_map()
        rank_map = build_rank_map_from_count_map(count_map)
        self.zipf_map = build_zipf_map(count_map, rank_map)
        
        if self.vocab is None:
            self.vocab = self.create_vocab(self.raw_programs)
        
        self.w2i, self.i2w = self.vocab['w2i'], self.vocab['i2w']
        self.vocab_size = len(self.w2i)

        self.sequences, self.lengths = self.process_programs(self.raw_programs)
        
        if self.raw_labels is not None:
            self.labels = self.process_labels(self.raw_labels)

    def create_vocab(self, data):
        assert self.split == 'train', \
            "Vocabulary can only be created for training file."

        w2c = OrderedCounter()
        w2i, i2w = dict(), dict()

        special_tokens = [PAD_TOKEN, UNK_TOKEN, SOS_TOKEN, EOS_TOKEN]
        for st in special_tokens:
            i2w[len(w2i)] = st
            w2i[st] = len(w2i)

        for program in self.data:
            tokens = program.split()
            w2c.update(tokens)

        for w, c in w2c.items():
            if c > self.min_occ:
                i2w[len(w2i)] = w
                w2i[w] = len(w2i)

        assert len(w2i) == len(i2w)
        print("Vocabulary of %i keys created." % len(w2i))

        self.vocab = dict(w2i=w2i, i2w=i2w)

    def process_programs(self, data):
        seqs, lengths = [], []

        n = len(data)
        for i in xrange(n):
            program = data[i]
            tokens = program.split()
            tokens = [SOS_TOKEN] + tokens[:self.max_seq_len] + [EOS_TOKEN]
            length = len(tokens)

            tokens.extend([PAD_TOKEN] * (self.max_seq_len + 2 - length))
            tokens = [self.w2i.get(token, self.w2i[UNK_TOKEN]) for token in tokens]

            seqs.append(tokens)
            lengths.append(length)

        return seqs, lengths

    def process_labels(self, annotations):
        raise NotImplementedError

    def build_count_map(self):
        # build a map from program to frequency
        with open('%s/sources-%d.pickle' % (self.data_dir, self.problem_id)) as fp:
            sources = cPickle.load(fp)
        with open('%s/countMap-%d.pickle' % (self.data_dir, self.problem_id)) as fp:
            counts = cPickle.load(fp)

        count_map = defaultdict(lambda: 0)
        domain = sources.keys()
        
        for i in domain:
            ast = sources[i]
            removeColors(ast)
            ast = flatten_ast(ast)
            program = ' '.join(ast)
            count_map[program] += counts[i]

        return dict(count_map)

    def __len__(self):
        return len(self.sequences)

    def __getitem__(self, index):
        seq, length = self.sequences[index], self.lengths[index]
        seq = torch.from_numpy(np.array(seq)).long()
        zipf = self.zipf_map[self.raw_programs[index]]
        
        if self.raw_labels is None:
            return seq, length, None, zipf
        else:
            label = self.labels[index]
            return seq, length, label, zipf


def build_rank_map_from_count_map(count_map):
    programs = count_map.keys()
    counts = count_map.values()
    # NOTE: need to reverse b/c most common = rank 0
    ranks = np.argsort(counts)[::-1]
    programs = np.array(programs)
    programs = programs[ranks]
    return dict(zip(programs, np.arange(len(programs))))


def build_zipf_map(count_map, rank_map):
    programs = np.asarray(count_map.keys())
    counts = np.asarray(count_map.values())
    ranks = np.asarray(rank_map.values())

    # by default, everything is a body
    zipf_classes = np.ones(len(programs)) * ZIPF_CLASS['body']
    # every with less than 3 counts is in the tail
    zipf_classes[counts < 3] = ZIPF_CLASS['tail']
    # every in top 20 is in the head
    zipf_classes[ranks <= 20] = ZIPF_CLASS['head']

    return dict(zip(programs, zipf_classes))
