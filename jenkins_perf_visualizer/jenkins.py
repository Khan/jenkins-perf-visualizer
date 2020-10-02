"""Helper functions for fetching data from Jenkins.

The main exported symbol here is the get_client_via_*(), which returns
a JenkinsFetcher object that other modules can use to fetch data from
Jenkins.

This module also has support for various methods of fetching the secret key
used to access Jenkins.
"""
import base64
import json
import os
import subprocess
try:
    from urllib import request  # python3
except ImportError:
    import urllib2 as request   # python2


HTTPError = request.HTTPError  # re-export for convenience


class JenkinsFetcher(object):
    def __init__(self, username, password):
        """The username and password of a Jenkins user with permissions.

        This user should have the ability to read information about all
        builds.

        DO NOT USE A LOGIN PASSWORD!  Instead create an API token
        and use that as the password.
        """
        if not username:
            raise ValueError("Must specify jenkins username")
        self.username = username
        self.password = password
        self.base = 'https://jenkins.khanacademy.org'

    def fetch(self, url_path):
        req = request.Request(self.base + url_path)
        auth = base64.b64encode(
            ('%s:%s' % (self.username, self.password)).encode('ascii'))
        req.add_header("Authorization", b"Basic " + auth)
        # Ended up not being needed, but it can't hurt!
        req.add_header("Cookie", "jenkins-timestamper=elapsed")

        r = request.urlopen(req)
        assert r.code == 200
        return r.read()

    def fetch_for_job(self, job_name, build_id, url_suffix):
        """Download jenkins/job_name/build_id/suffix.

        job_name is, e.g. 'deploy/build-webapp'
        """
        url_path = os.path.join(('/' + job_name).replace('/', '/job/'),
                                str(build_id) if build_id is not None else '',
                                url_suffix)
        return self.fetch(url_path)

    def fetch_pipeline_steps(self, job_name, build_id):
        """Download the pipeline-steps html page for the given job/id.

        NOTE: it would be more principled to use the API for this;
        while it's under-documented at best, there are some pointers at
           https://issues.jenkins-ci.org/browse/JENKINS-29188
        We could get the tree structure from
           <build-url>/api/json?tree=actions[nodes[displayName,id,parents]]
        and the timing data from
           <build-url>/execution/node/<step-id>/wfapi/

        But the API doesn't provide execution as a tree -- it sees it as
        linear, except for parallel() nodes -- and doesn't aggregate times
        for blocks, both of which we need, both of which the "pipeline steps"
        html page does for us, and and both of which are surprisingly
        difficult to do correctly.  So we just go and scrape the html page.

        job_name is, e.g. 'deploy/build-webapp'
        """
        text = self.fetch_for_job(job_name, build_id, 'flowGraphTable')
        return text.decode('utf-8')

    def fetch_job_start_time(self, job_name, build_id, root_step):
        """We need to know the node-id of the start-step of this job."""
        s = self.fetch_for_job(job_name, build_id,
                               'execution/node/%s/wfapi/' % root_step.id)
        data = json.loads(s)
        return data["startTimeMillis"] / 1000.0

    def fetch_job_parameters(self, job_name, build_id):
        """Fetch the jenkins parameters that this job was run with."""
        s = self.fetch_for_job(job_name, build_id,
                               'api/json?tree=actions[parameters[name,value]]')
        data = json.loads(s)
        params = next(
            (a for a in data.get('actions', {})
             if a.get('_class') == 'hudson.model.ParametersAction'),
            {}
        )
        return {e["name"]: e["value"] for e in params.get('parameters', {})}

    def fetch_all_build_ids(self, job_name):
        """Fetch all the build-ids jenkins has for a given job."""
        s = self.fetch_for_job(job_name, None,
                               'api/json?tree=allBuilds[number]')
        data = json.loads(s)
        return [b['number'] for b in data['allBuilds']]


def get_client_via_password(username, password):
    """Create and return a JenkinsFetcher with given username/password."""
    return JenkinsFetcher(username, password)


def get_client_via_keeper(keeper_record_id):
    """Create a JenkinsFetcher, getting username/password from Keeper Security.

    This requires you to have the keepercommander CLI tool installed:
        https://github.com/Keeper-Security/Commander
    The record-id should have the jenkins username in the "login"
    field and the api token in the "password" field.

    Your keeper configuration file, allowing non-interactive use
    of the keepercommander tool, must live in ~/.keeper-config.json.
    """
    text = subprocess.check_output([
        'keeper',
        '--config=%s/.keeper-config.json' % os.getenv("HOME"),
        '--format=json',
        'g',
        keeper_record_id,
    ])
    record = json.loads(text)
    return JenkinsFetcher(record['login'], record['password'])
