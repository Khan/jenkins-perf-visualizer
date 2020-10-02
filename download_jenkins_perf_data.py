#!/usr/bin/env python

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
import multiprocessing.pool

from jenkins_perf_visualizer import fetch
from jenkins_perf_visualizer import jenkins


# TODO(csilvers): move to a config file
KEEPER_RECORD_ID = 'mHbUyJXAmnZyqLY3pMUmjQ'


def _download_one_build(param):
    # (The weird parameter format is because this is used by Pool().)
    (job, build_id, output_dir, jenkins_client, force) = param
    print("Fetching %s:%s" % (job, build_id))
    try:
        (_, job_params, job_start_time, outfile) = fetch.fetch_build(
            job, build_id, output_dir, jenkins_client, force)
    except fetch.DataError as e:
        print("ERROR: skipping %s:%s: %s" % (e.job, e.build_id, e))
        return

    # Now create a symlink organized by date and title.
    # TODO(csilvers): re-enable this via config settings.
    '''
    yyyy_mm = time.strftime("%Y-%m", time.localtime(job_start_time))
    title = job_params.get('REVISION_DESCRIPTION', '<unknown job>')
    category_dir = os.path.join(output_dir, '%s.%s' % (yyyy_mm, title))
    symlink = os.path.join(category_dir, os.path.basename(outfile))
    if force and os.path.exists(symlink):
        os.unlink(symlink)
    if not os.path.exists(symlink):
        mkdir_p(category_dir)
        os.symlink(os.path.relpath(outfile, os.path.dirname(symlink)),
                   symlink)
    '''


def download_builds(builds, output_dir, jenkins_username, jenkins_password,
                    force=False):
    """Download and save the data-needed-to-render for biulds and jobs.

    We ask jenkins what builds it knows about for the given jobs,
    then download them all to get a `.data` file that is suitable
    for passing as input to this script (at some later date) to
    graph this build.

    Arguments:
        builds: a list of either builds or jobs, e..g
                  ["deploy/build-webapp:1214", "deploy/webapp-test"]
                For jobs (where the build-id is omitted), we fetch all
                builds for the given job that Jenkins knows about.
        output_dir: the directory to put all the data files
        jenkins_username: a valid jenkins username
        jenkins_password: a valid API token.  If None, fetch from keeper.
        force: if False, don't fetch any jobs that already have a
               data-file in output_dir.  If True, fetch everything.
    """
    # TODO(csilvers): use config options to decide what to do, instead.
    if jenkins_password:
        jenkins_client = jenkins.get_client_via_password(
            jenkins_username, jenkins_password)
    else:
        jenkins_client = jenkins.get_client_via_keeper(KEEPER_RECORD_ID)
    for build in builds:
        if ':' in build:
            (job, build_id) = build.split(':')
            build_ids = [build_id]
        else:
            job = build
            build_ids = jenkins_client.fetch_all_build_ids(job)

        pool = multiprocessing.pool.ThreadPool(7)  # pool size is arbitrary
        pool.map(_download_one_build,
                 [(job, b, output_dir, jenkins, force) for b in build_ids])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'build_or_job', nargs='+',
        help=("Jenkins builds to fetch, e.g. deploy/build-webapp:1543, "
              "OR jenkins jobs to fetch all builds of, e.g. webapp-test."))
    parser.add_argument('--jenkins-username',
                        default='jenkins@khanacademy.org')
    parser.add_argument('--jenkins-pw',
                        help=('API token that gives access to job data. '
                              'If not set, fetch the secret from keeper '
                              '(record %s)' % KEEPER_RECORD_ID))
    parser.add_argument('-d', '--output-dir',
                        default='/tmp/jenkins-job-perf-analysis',
                        help='Directory to write the output data files')
    parser.add_argument('--force', action='store_true',
                        help=('If set, re-fetch data files even if they '
                              'already exist in output-dir.'))
    args = parser.parse_args()

    download_builds(args.build_or_job, args.output_dir,
                    args.jenkins_username, args.jenkins_pw, args.force)
