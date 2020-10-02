"""Routines to fetch data from Jenkins and save it to a data file.

The output data file has all the information needed to make a
visualization graph, and can be given as input to the visualizer
script.  It is mostly the html of the "pipeline steps" Jenkins page
for a build, with some additional metadata thrown in.

TODO(csilvers): save two data files, the raw datafile and the json.
"""
import errno
import json
import os
import re

from jenkins_perf_visualizer import jenkins
from jenkins_perf_visualizer import steps


class DataError(Exception):
    """An error reading or parsing Jenkins data for a build."""
    def __init__(self, job, build_id, message):
        super(DataError, self).__init__(message)
        self.job = job
        self.build_id = build_id


def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def fetch_from_datafile(fname):
    """Fetch data from the cached data-file, instead of asking Jenkins.

    The datafile is one that was saved via a previous call to fetch_build().
    """
    with open(fname, 'rb') as f:
        step_html = f.read().decode('utf-8')
    m = re.search(r'<script>var parameters = (.*?)</script>',
                  step_html)
    build_params = json.loads(m.group(1) if m else '{}')
    # We get the buid-start time by the file's mtime.
    build_start_time = os.path.getmtime(fname)
    return (step_html, build_params, build_start_time)


def _fetch_from_jenkins(job, build_id, jenkins_client):
    """Fetch data for the given build from Jenkins."""
    try:
        build_params = jenkins_client.fetch_build_parameters(job, build_id)
        step_html = jenkins_client.fetch_pipeline_steps(job, build_id)
        step_root = steps.parse_pipeline_steps(step_html)
        if not step_root:
            raise DataError(job, build_id, "invalid job? (no steps found)")
        build_start_time = jenkins_client.fetch_build_start_time(
            job, build_id, step_root.id)
        return (step_html, build_params, build_start_time)
    except jenkins.HTTPError as e:
        raise DataError(job, build_id, "HTTP error: %s" % e)


def fetch_build(job, build_id, output_dir, jenkins_client, force=False):
    """Download, save, and return the data-needed-to-render for one build."""
    mkdir_p(output_dir)
    outfile = os.path.join(
        output_dir, '%s:%s.data' % (job.replace('/', '--'), build_id))

    if not force and os.path.exists(outfile):
        (step_html, build_params, build_start_time) = fetch_from_datafile(
            outfile)
    else:
        (step_html, build_params, build_start_time) = _fetch_from_jenkins(
            job, build_id, jenkins_client)

        with open(outfile, 'wb') as f:
            f.write(step_html.encode('utf-8'))
            params_text = ('\n\n<script>var parameters = %s</script>'
                           % json.dumps(build_params))
            f.write(params_text.encode('utf-8'))
        # Set the last-modified time of this file to its start-time.
        os.utime(outfile, (build_start_time, build_start_time))

    return (step_html, build_params, build_start_time, outfile)
