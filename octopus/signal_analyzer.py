#
# Copyright (c) 2018 Hyeshik Chang
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

from pomegranate import (
    HiddenMarkovModel, GeneralMixtureModel, State, NormalDistribution)
from weakref import proxy
from itertools import groupby
from ont_fast5_api.fast5_file import Fast5File
from ont_fast5_api.analysis_tools.basecall_1d import Basecall1DTools
import numpy as np
import pandas as pd
import os
import h5py
from scipy.signal import medfilt
from .utils import union_intervals

__all__ = ['SignalAnalyzer', 'SignalAnalysis']


class PipelineHandledError(Exception):
    pass


class SignalAnalyzer:

    def __init__(self, config, batchid):
        self.config = config
        self.segmodel = load_segmentation_model(config['segmentation_model'])
        self.unsplitmodel = load_segmentation_model(config['unsplit_read_detection_model'])
        self.kmermodel = pd.read_table(config['kmer_model'], header=0, index_col=0)
        self.kmersize = len(self.kmermodel.index[0])
        self.batchid = batchid

        if config['dump_adapter_signals']:
            self.adapter_dump_file, self.adapter_dump_group = (
                self.open_adapter_dump_file(config['outputdir'], batchid))
            self.adapter_dump_list = []
        else:
            self.adapter_dump_file = self.adapter_dump_group = None

    def process(self, filename, outputprefix):
        return SignalAnalysis(filename, outputprefix, self).process()

    def open_adapter_dump_file(self, outputdir, batchid):
        h5filename = os.path.join(outputdir, 'adapter-dumps', '{:08d}.h5'.format(batchid))
        h5 = h5py.File(h5filename, 'w')
        h5group = h5.create_group('adapter/{:08d}'.format(batchid))
        return h5, h5group

    def push_adapter_signal_catalog(self, read_id, adapter_start, adapter_end):
        self.adapter_dump_list.append((read_id, adapter_start, adapter_end))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.adapter_dump_file is not None:
            catgrp = self.adapter_dump_file.create_group('catalog/adapter')
            encodedarray = np.array(self.adapter_dump_list,
                dtype=[('readid', 'S36'), ('start', 'i8'), ('end', 'i8')])
            try:
                catgrp.create_dataset(format(self.batchid, '08d'), shape=encodedarray.shape,
                                      data=encodedarray)
            except:
                import traceback
                traceback.print_exc() # XXX: check duplicated reads
            self.adapter_dump_file.close()


class SignalAnalysis:

    def __init__(self, filename, outputprefix, analyzer):
        self.filename = filename
        self.outputprefix = outputprefix
        self.config = analyzer.config['head_signal_processing']
        self.analyzer = proxy(analyzer)
        self.fast5 = None
        self.sequence = None
        self.open_data_files(filename)

    def __enter__(self):
        return self

    def __del__(self):
        if self.fast5 is not None:
            self.fast5.close()
        return False

    def open_data_files(self, filename):
        self.fast5 = Fast5File(filename, 'r')
        self.read_id = self.fast5.status.read_info[0].read_id
        self.sampling_rate = self.fast5.get_channel_info()['sampling_rate']

    def load_events(self):
        assert self.fast5 is not None

        # Load events (15-sample chunks in albacore).
        with Basecall1DTools(self.fast5) as bcall:
            events = bcall.get_event_data('template')
            if events is None:
                raise PipelineHandledError('not_basecalled')

            events = pd.DataFrame(events)
            events['pos'] = np.cumsum(events['move'])

            self.sequence = bcall.get_called_sequence('template')[1:]

        # Rescale the signal to fit in the kmer models
        scaling_params = self.compute_scaling_parameters(events)

        duration = np.hstack((np.diff(events['start']), [1])).astype(np.uint64)
        events['end'] = events['start'] + duration

        events['scaled_mean'] = np.poly1d(scaling_params)(events['mean'])

        return events

    def compute_scaling_parameters(self, events):
        # Get median value for each kmer state and match with the ONT kmer model.
        events_summarized = events.groupby('pos', sort=False,
                                           group_keys=False, as_index=False).agg(
                                           {'mean': 'median', 'model_state': 'first'})
        if len(events_summarized) < self.config['minimum_kmer_states']:
            raise PipelineHandledError('too_few_kmer_states')

        ev_with_mod = pd.merge(events_summarized, self.analyzer.kmermodel,
                               how='left', left_on='model_state', right_index=True)

        # Filter possible outliers out
        meanratio = ev_with_mod['mean'] / ev_with_mod['level_mean']
        assert len(self.config['scaler_outlier_trim_range']) == 2
        inliers = ev_with_mod[meanratio.between(*
                              np.percentile(meanratio, self.config['scaler_outlier_trim_range']))]

        # Do the final regression
        return np.polyfit(inliers['mean'], inliers['level_mean'], 1)

    def load_raw_signal(self, scaler, start, end):
        end = min(int(self.fast5.status.read_info[0].duration), end)
        raw_sig = self.fast5.get_raw_data(start=start, end=end, scale=True)
        return scaler(medfilt(raw_sig, self.config['median_filter_size']))

    def detect_segments(self, events):
        headsig = events['scaled_mean'][
                    :(events['pos'] <= self.config['segmentation_scan_limit']).sum()]

        # Run Viterbi fitting to signal model
        plogsum, statecalls = self.analyzer.segmodel.viterbi(headsig)

        # Summarize state transitions
        sigparts = {}
        for _, positions in groupby(enumerate(statecalls[1:]),
                                    lambda st: id(st[1][1])):
            first, state = last, _ = next(positions)
            statename = state[1].name
            #statename = statecalls[1 + first].name
            for last, _ in positions:
                pass
            sigparts[statename] = (first, last) # right-inclusive

        return sigparts

    def detect_unsplit_read(self, events, segments):
        # Detect if the read contains two or more adapters in a single read.
        try:
            payload_start = events.iloc[segments['adapter'][1] + 1]['start']
        except IndexError:
            return False # Must be an adapter-only read

        # Bind settings into the local namespace
        config = self.config['unsplit_read_detection']
        _ = lambda name: int(config[name] * self.sampling_rate)
        window_size = _('window_size'); window_step = _('window_step')
        strict_duration = _('strict_duration')
        duration_cutoffs = [
            (_('loosen_full_length'), _('loosen_dna_length')),
            (_('strict_full_length'), _('strict_dna_length'))]

        excessive_adapters = []

        for left in range(payload_start, events.iloc[-1]['end'], window_step):
            evblock = events[events['start'].between(left, left + window_size)]
            if 0:
                from matplotlib import pyplot as plt
                fig, axes = plt.subplots(2, 1, figsize=(6, 4))
                axes[0].plot(events['mean'].tolist())
                axes[1].plot(evblock['mean'].tolist())
                plt.show()

            if len(evblock) < 1:
                break

            _, statecalls = self.analyzer.unsplitmodel.viterbi(evblock['scaled_mean'])
            leader_start = None

            # Find two contiguous states leaders -> adapter and compute sum of the durations
            for _, positions in groupby(enumerate(statecalls[1:]), lambda st: id(st[1][1])):
                first, state = last, _ = next(positions)
                statename = state[1].name
                if statename not in ('adapter', 'leader-high', 'leader-low'):
                    leader_start = None
                    continue

                # Find the last index of matching state calls
                for last, _ in positions:
                    pass

                if leader_start is None:
                    leader_start = first

                if statename != 'adapter':
                    continue

                adapter_end = int(evblock.iloc[last]['end'])
                leader_start_in_read = int(evblock.iloc[leader_start]['start'])
                total_duration = adapter_end - leader_start_in_read
                adapter_duration = adapter_end - evblock.iloc[first]['start']
                total_cutoff, adapter_cutoff = duration_cutoffs[
                        (leader_start_in_read - payload_start) <= strict_duration]

                if total_duration >= total_cutoff and adapter_duration >= adapter_cutoff:
                    excessive_adapters.append([leader_start_in_read, 1 + adapter_end])

                leader_start = None

        if not excessive_adapters:
            return False

        adapter_intervals = (
            [[0, payload_start]] + union_intervals(excessive_adapters)
            + [[np.inf, np.inf]])
        basequality_cutoff = config['basecount_quality_limit']
        count_high_quality_reads = lambda tbl: (
            (tbl.groupby('pos').agg({'p_model_state': 'max'})['p_model_state']
                > basequality_cutoff).sum() if len(tbl) >= 0 else 0)
        subread_lengths = [
            count_high_quality_reads(events[events['start'].between(left, right)])
            for (_, left), (right, _) in zip(adapter_intervals[0:], adapter_intervals[1:])]

        subread_hq_length_total = sum(subread_lengths[1:])

        if (subread_hq_length_total > config['subread_basecount_limit'] or
                (subread_hq_length_total + 1) / (subread_lengths[0] + 1)
                    > config['subread_baseratio_limit']):
            return True

        return False

    def trim_adapter(self, events, segments):
        if self.sequence is None:
            return

        adapter_end = segments['adapter'][1]
        kmer_lead_size = self.analyzer.kmersize // 2
        adapter_basecall_length = events.iloc[adapter_end]['pos'] + kmer_lead_size

        if adapter_basecall_length > len(self.sequence[0]):
            raise PipelineHandledError('basecall_table_incomplete')
        elif adapter_basecall_length > 0:
            self.sequence = (
                self.sequence[0][:-adapter_basecall_length],
                self.sequence[1][:-adapter_basecall_length])

    def process(self):
        error_set = None

        try:
            events = self.load_events()

            segments = self.detect_segments(events)
            if 'adapter' not in segments:
                raise PipelineHandledError('adapter_not_detected')

            if self.analyzer.config['trim_adapter']:
                self.trim_adapter(events, segments)

            if self.analyzer.config['filter_unsplit_reads']:
                isunsplit_read = self.detect_unsplit_read(events, segments)
                if isunsplit_read:
                    raise PipelineHandledError('unsplit_read')

            if self.analyzer.config['barcoding']:
                raise NotImplementedError
            else:
                outname = 'pass'

        except PipelineHandledError as exc:
            outname = 'artifact' if exc.args[0] in ('unsplit_read',) else 'fail'
            error_set = exc.args[0]
        else:
            if self.analyzer.config['dump_adapter_signals']:
                self.dump_adapter_signal(events, segments)

        return {
            'read_id': self.read_id,
            'filename': self.filename,
            'errors': error_set,
            'output_label': outname,
            'fastq': self.sequence,
        }

    def dump_adapter_signal(self, events, segments):
        adapter_events = events.iloc[slice(*segments['adapter'])]
        if len(adapter_events) > 0:
          try:
            self.analyzer.adapter_dump_group.create_dataset(self.read_id,
                shape=(len(adapter_events),), dtype=np.float32,
                data=adapter_events['scaled_mean'])
            self.analyzer.push_adapter_signal_catalog(self.read_id,
                adapter_events['start'].iloc[0], adapter_events['end'].iloc[-1])
          except:
              import traceback
              traceback.print_exc() # XXX: check duplicated reads


# Internal serialization implementation pomegranate to json does not accurately
# recover the original. Use a custom format here.
def load_segmentation_model(modeldata):
    model = HiddenMarkovModel('model')

    states = {}
    for s in modeldata:
        if len(s['emission']) == 1:
            emission = NormalDistribution(*s['emission'][0][:2])
        else:
            weights = np.array([w for _, _, w in s['emission']])
            dists = [NormalDistribution(mu, sigma)
                     for mu, sigma, _ in s['emission']]
            emission = GeneralMixtureModel(dists, weights=weights)
        state = State(emission, name=s['name'])

        states[s['name']] = state
        model.add_state(state)
        if 'start_prob' in s:
            model.add_transition(model.start, state, s['start_prob'])

    for s in modeldata:
        current = states[s['name']]
        for nextstate, prob in s['transition']:
            model.add_transition(current, states[nextstate], prob)

    model.bake()

    return model

