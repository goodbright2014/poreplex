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

import asyncio
import signal
import argparse
import sys
import os
import yaml
from progressbar import ProgressBar, NullBar, UnknownLength
from concurrent.futures import (
    ProcessPoolExecutor, CancelledError, ThreadPoolExecutor)
from . import __version__
from .signal_analyzer import SignalAnalyzer


def errx(msg):
    errprint(msg)
    sys.exit(254)

def errprint(msg):
    print(msg, file=sys.stderr)

def taskmgr_exit(signalname, loop, session):
    if session['running']:
        errprint("\nTermination request in process. Please wait for a moment.")
        session['running'] = False
    for task in asyncio.Task.all_tasks():
        task.cancel()

    loop.stop()

def taskmgr_errx(loop, session, message):
    if session['running']:
        errprint(message)
        taskmgr_exit('ERROR', loop, session)


def process_file(inputfile, outputprefix, analyzer):
    result = analyzer.process(inputfile, outputprefix)
    return result


def process_batch(batchid, reads, config):
    with SignalAnalyzer(config, batchid) as analyzer:
        for f5file in reads:
            # =-= XXX temporary
            #if os.path.exists(outputprefix + '.npy'):
            #    continue
            # XXX
            process_file(f5file, 'x', analyzer)

#    def show_memory_usage():
#        usages = open('/proc/self/statm').read().split()
#        print('{:05d} MEMORY total={} RSS={} shared={} data={}'.format(
#                batchid, usages[0], usages[1], usages[2], usages[4]))
#    show_memory_usage()

    return len(reads), 0


async def run_process_batch(loop, executor, batch_id, files, session, config):
    try:
        nsucc, nfail = await loop.run_in_executor(executor, process_batch, batch_id,
                                                  files, config)
    except CancelledError:
        return

    session['reads_succeed'] += nsucc
    session['reads_failed'] += nfail
    session['reads_queued'] -= nsucc + nfail


def scan_dir_recursive_worker(dirname, suffix='.fast5'):
    files, dirs = [], []
    for entryname in os.listdir(dirname):
        if entryname.startswith('.'):
            continue

        fullpath = os.path.join(dirname, entryname)
        if os.path.isdir(fullpath):
            dirs.append(entryname)
        elif entryname.lower().endswith(suffix):
            files.append(entryname)

    return dirname, dirs, files


async def scan_dir_recursive(loop, executor_io, executor_compute, dirname,
                             config, jobstack=None, session=None,
                             top=False):
    if not session['running']:
        return

    try:
        errormsg = None
        path, dirs, files = await loop.run_in_executor(executor_io,
            scan_dir_recursive_worker, dirname)
    except CancelledError as exc:
        if top: return
        else: raise exc
    except Exception as exc:
        errormsg = str(exc)

    if errormsg is not None:
        return taskmgr_errx(loop, session, 'ERROR: ' + str(errormsg))

    def flush():
        if session['running'] and jobstack:
            batch_id = session['next_batch_id']
            session['next_batch_id'] += 1
            work = run_process_batch(loop, executor_compute, batch_id,
                                     jobstack[:], session, config)
            loop.create_task(work)
            del jobstack[:]

    if top:
        jobstack = []

    batch_chunk_size = config['batch_chunk_size']

    for filename in files:
        fullpath = os.path.join(path, filename)
        jobstack.append(fullpath)
        session['reads_queued'] += 1
        session['reads_found'] += 1
        if len(jobstack) >= batch_chunk_size:
            flush()

    try:
        for subdir in dirs:
            subdirpath = os.path.join(path, subdir)
            await scan_dir_recursive(loop, executor_io, executor_compute,
                                     subdirpath, config, jobstack, session)
    except CancelledError as exc:
        if top: return
        else: raise exc

    if top:
        if jobstack:
            flush()
        session['scan_finished'] = True


async def monitor_progresses(loop, session, config):
    pbarclass = NullBar if config['quiet'] else ProgressBar
    pbar = pbarclass(max_value=UnknownLength, initial_value=0)
    pbar.start()
    pbar_growing = True

    while session['running']:
        finished_jobs = session['reads_succeed'] + session['reads_failed']

        if pbar_growing and session['scan_finished']:
            pbar_growing = False
            pbar = pbarclass(max_value=session['reads_found'],
                               initial_value=finished_jobs)
            pbar.start()
        else:
            pbar.update(finished_jobs)

        await asyncio.sleep(0.3)

        if session['scan_finished'] and session['reads_queued'] <= 0:
            break


def force_terminate_executor(executor):
    executor._call_queue.empty()

    if executor._processes:
        alive_pids = set(
            pid for pid, proc in executor._processes.items()
            if proc.is_alive())
        for pid in alive_pids:
            os.kill(pid, signal.SIGKILL)


def taskmgr_main(config, args):
    session = {
        'running': True, 'scan_finished': False,
        'reads_queued': 0, 'reads_found': 0,
        'reads_succeed': 0, 'reads_failed': 0,
        'next_batch_id': 0}

    loop = asyncio.get_event_loop()
    io_threads = 2

    with ProcessPoolExecutor(args.parallel) as executor_compute, \
            ThreadPoolExecutor(io_threads) as executor_io:
        for signame in 'SIGINT SIGTERM'.split():
            loop.add_signal_handler(getattr(signal, signame), taskmgr_exit,
                    signame, loop, session)

        monitor_task = loop.create_task(monitor_progresses(loop, session, config))

        scanjob = scan_dir_recursive(loop, executor_io, executor_compute,
                args.input, config, top=True, session=session)
        loop.create_task(scanjob)

        try:
            loop.run_until_complete(monitor_task)
        except CancelledError:
            errprint('\nInterrupted')
        except Exception as exc:
            errprint('\nERROR: ' + str(exc))

        force_terminate_executor(executor_compute)

        for task in asyncio.Task.all_tasks():
            if not (task.done() or task.cancelled()):
                try:
                    loop.run_until_complete(task)
                except CancelledError:
                    errprint('\nInterrupted')
                except Exception as exc:
                    errprint('\nERROR: ' + str(exc))

    loop.close()


def show_banner():
    print("""
\x1b[1mOctopus\x1b[0m version {version} by Hyeshik Chang
- A demultiplexer for nanopore direct RNA sequencing
""".format(version=__version__))


def load_config(args):
    presets_dir = os.path.join(os.path.dirname(__file__), 'presets')
    if not args.config:
        config_path = os.path.join(presets_dir, 'rna-r941.cfg')
    elif os.path.isfile(args.config):
        config_path = args.config
    elif os.path.isfile(os.path.join(presets_dir, args.config + '.cfg')):
        config_path = os.path.join(presets_dir, args.config + '.cfg')
    else:
        errx('ERROR: Cannot find a configuration in {}.'.format(args.config))

    config = yaml.load(open(config_path))
    kmer_models_dir = os.path.join(os.path.dirname(__file__), 'kmer_models')
    if not os.path.isabs(config['kmer_model']):
        config['kmer_model'] = os.path.join(kmer_models_dir, config['kmer_model'])

    return config


def create_output_directories(outputdir, config):
    subdirs = ['fastq', 'tmp']

    if config['fast5_output']:
        subdirs.extend(['fast5'])

    if config['dump_adapter_signals']:
        subdirs.extend(['adapter-dumps'])

    for subdir in subdirs:
        fullpath = os.path.join(outputdir, subdir)
        if not os.path.isdir(fullpath):
            os.makedirs(fullpath)


def main(args):
    if not args.quiet:
        show_banner()

    if not os.path.isdir(args.input):
        errx('ERROR: Cannot open the input directory {}.'.format(args.input))

    if not os.path.isdir(args.output):
        try:
            os.makedirs(args.output)
        except:
            errx('ERROR: Failed to create the output directory {}.'.format(args.output))

    config = load_config(args)
    config['quiet'] = args.quiet
    config['outputdir'] = args.output
    config['filter_unsplit_reads'] = not args.keep_unsplit
    config['batch_chunk_size'] = args.batch_chunk
    config['dump_adapter_signals'] = args.dump_adapter_signals
    config['fast5_output'] = args.fast5
    config['fast5_always_symlink'] = args.always_symlink_fast5

    create_output_directories(args.output, config)

    taskmgr_main(config, args)


def __main__():
    parser = argparse.ArgumentParser(
        prog='octopus',
        description='Barcode demultiplexer for nanopore direct RNA sequencing')

    parser.add_argument('-i', '--input', required=True,
                        help='Path to the directory with the input FAST5 files.')
    parser.add_argument('-o', '--output', required=True,
                        help='Output directory path')
    parser.add_argument('-p', '--parallel', default=1, type=int,
                        help='Number of worker processes (default: 1)')
    parser.add_argument('--batch-chunk', default=128, type=int,
                        help='Number of files in a single batch (default: 128)')
    parser.add_argument('-c', '--config', default='',
                        help='Path to signal processing configuration.')
    parser.add_argument('--keep-unsplit', default=False, action='store_true',
                        help="Don't remove unsplit reads fused of two or more RNAs in output.")
    parser.add_argument('--albacore', default=False, action='store_true',
                        help='Call albacore for basecalling on-the-fly.')
    parser.add_argument('--dump-adapter-signals', default=False, action='store_true',
                        help='Dump adapter signal dumps for training')
    parser.add_argument('--dump-basecalled-events', default=False, action='store_true',
                        help='Dump basecalled events from albacore to the output')
    parser.add_argument('-5', '--fast5', default=False, action='store_true',
                        help='Link or copy FAST5 files to separate output directories.')
    parser.add_argument('--always-symlink-fast5', default=False, action='store_true',
                        help='Create symbolic links to FAST5 files in output directories '
                             'even when hard linking is possible.')
    parser.add_argument('-q', '--quiet', default=False, action='store_true',
                        help='Suppress non-error messages.')

    args = parser.parse_args(sys.argv[1:])
    main(args)
