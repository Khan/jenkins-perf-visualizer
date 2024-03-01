#!/usr/bin/env python3

"""Download and save the data-needed-to-render.

This is used to read data about all the builds that Jenkins currently
knows about.  It can store the data in a .data file, so even after
Jenkins expires the build it will still be possible to analyze it.

It is useful to run this in a cron job every few minutes, to automatically
download all data for jobs of interest.

TODO(csilvers): also provide the ability to expire old data, perhaps having a
max-directory-size.
"""
import argparse
import logging
import os
import time

from jenkins_perf_visualizer import configuration
from jenkins_perf_visualizer import fetch
from jenkins_perf_visualizer import jenkins


def _download_one_build(param):
    # (The weird parameter format is because this is used by Pool().)
    (job, build_id, output_dir, jenkins_client, grouping_param, force) = param
    try:
        (_, build_params, build_start_time, outfile) = fetch.fetch_build(
            job, build_id, output_dir, jenkins_client, force)
    except fetch.DataError as e:
        logging.error("Skipping %s:%s: %s", e.job, e.build_id, e)
        return

    # Now create a symlink farm grouping together jobs with the same
    # grouping parameter.
    if not grouping_param:
        return

    yyyy_mm = time.strftime("%Y-%m", time.localtime(build_start_time))
    grouping_id = build_params.get(grouping_param, 'UNKNOWN_GROUP')
    category_dir = os.path.join(output_dir, '%s.%s' % (yyyy_mm, grouping_id))
    symlink = os.path.join(category_dir, os.path.basename(outfile))
    if force and os.path.exists(symlink):
        os.unlink(symlink)
    if not os.path.exists(symlink):
        try:
            os.makedirs(os.path.dirname(symlink))
        except OSError as e:
            pass
        os.symlink(os.path.relpath(outfile, os.path.dirname(symlink)),
                   symlink)


def download_builds(config, builds, force=False):
    """Download and save the data-needed-to-render for builds and jobs.

    We ask jenkins what builds it knows about for the given jobs,
    then download them all to get a `.data` file that is suitable
    for passing as input to this script (at some later date) to
    graph this build.

    Arguments:
        config: a config.json object
        builds: a list of either builds or jobs, e..g
                  ["deploy/build-webapp:1214", "deploy/webapp-test"]
                For jobs (where the build-id is omitted), we fetch all
                builds for the given job that Jenkins knows about.
        force: if False, don't fetch any builds that already have a
               data-file in output_dir.  If True, fetch everything.
    """
    if not config.get('datadir'):
        raise ValueError("No output dir (--datadir) specified")

    jenkins_client = jenkins.get_client(config)
    download_args = []
    for build in builds:
        if ':' in build:
            (job, build_id) = build.split(':')
            download_args.append(
                (job, build_id, config['datadir'],
                 jenkins_client, config.get('groupingParameter'), force)
            )
        else:
            job = build
            for build_id in jenkins_client.fetch_all_build_ids(job):
                download_args.append(
                    (job, build_id, config['datadir'],
                     jenkins_client, config.get('groupingParameter'), force)
                )

    num_threads = config.get('downloadThreads', 7)  # arbitrary number
    if num_threads <= 1:
        for args_tuple in download_args:
            _download_one_build(args_tuple)
    else:
        import multiprocessing.pool  # only import if we need it!
        pool = multiprocessing.pool.ThreadPool(num_threads)
        pool.map(_download_one_build, download_args)


if __name__ == '__main__':
    logging.basicConfig(format="[%(asctime)s %(levelname)s] %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        'build_or_job', nargs='+',
        help=("Jenkins builds to fetch, e.g. deploy/build-webapp:1543, "
              "OR jenkins jobs to fetch all builds of, e.g. webapp-test."))

    # Lets you specify a config file to control everything else.
    configuration.add_config_arg(parser)
    # Lets you override the values in the config file on a per-run basis.
    configuration.add_datadir_arg(parser)
    configuration.add_download_threads_arg(parser)

    parser.add_argument('--force', action='store_true',
                        help=('If set, re-fetch data files even if they '
                              'already exist in output-dir.'))
    parser.add_argument('-v', '--verbose', action='store_true',
                        help=('Log more data when running.'))

    args = parser.parse_args()
    config = configuration.load(args)

    logging.getLogger().setLevel(
        logging.DEBUG if args.verbose else logging.INFO)

    download_builds(config, args.build_or_job, args.force)
