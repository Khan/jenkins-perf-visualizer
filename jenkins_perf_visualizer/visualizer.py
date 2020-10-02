#!/usr/bin/python

"""Emit a flamechart that shows where time is spent during a deploy.

The output is a chart.  The x axis is seconds since the beginning
of the input jenkins job until the end.  (If multiple jobs are specified
it's the beginning of the first job until the end of the last one.)

The y axis is a set of "nodes".  A node captures the time taken by
a block of Jenkins pipeline steps:
   1) Every stage() step begins a new node, labeled with the name of
      the stage, and includes all the commands in that stage.
   2) Every branch of a parallel() step begins a new node, labeled
      with the name of that branch, and includes all the commands
      run by that branch.
   3) "<jenkins job name>" holds the overall jenkins job.
(Note that our helper jenkins functions, like notify() define
some stages of their own, so every Jenkins job has a "main"
node which contains everything inside a "notify" block.)

Each node can be in one of several states:
1) Running
2) Sleeping, via a sleep(), waitFor(), or prompt() jenkins step.
   Note that sleeping via shell (`sh("sleep 10")`) is *not*
   counted here -- we have no way of knowing what sh() is doing!
3) Waiting for an executor.  This happens when a node decides to
   start running on a jenkins worker, and it has to wait for a
   new gce machine to get started up.

Our output for each node is a horizontal bar, saying what the
node is doing during the given second.  Each node has its own
color, with varying brightnesses to say what the node is doing
(so if the coordinate (50, 6) is light yellow, it means that node
#6 is waiting for an executor during second 50, whereas if it's
dark yellow it's running during second 50).

This script works in three main stages:
1) It figures out what steps were run, and how long they took,
   by scraping the html from a jenkins /flowGraphTable
   ("Pipeline steps") page, and collects them into a runtime tree.
   (The `Step` class.)
2) It figures out which Node each part of that tree is running
   over (by looking for stage() and parallel() steps), and
   linearizes the steps in a Node into a set of time-ranges
   where that node is doing something.  It then looks at each
   of the steps in that node to categorize each moment of time
   into a category: running, sleeping, or waiting.
   (The `Node` class.)
3) It constructs and emits a graph based on the node data.
   (`create_html()`.)

# TODO(csilvers): rename 'timerange' into a better name
# TODO(csilvers): rename 'job' to 'build' when appropriate
"""
from __future__ import absolute_import

import errno
import json
import multiprocessing.pool
import os
import re
import time
import webbrowser

import builds
import jenkins
import nodes
import steps


# TODO(csilvers): move to a config file
KEEPER_RECORD_ID = 'mHbUyJXAmnZyqLY3pMUmjQ'


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


def create_html(job_datas):
    """Return an html page that will render our flame-like chart.

    We use custom CSS to do this.
    """
    deploy_start_time_ms = min(j.data['jobStartTimeMs'] for j in job_datas)
    deploy_end_time_ms = max(j.data['jobEndTimeMs'] for j in job_datas)
    title = ('%s (%s)'
             % (' + '.join(sorted(set(j.data['title'] for j in job_datas))),
                time.strftime("%Y/%m/%d %H:%M:%S",
                              time.localtime(deploy_start_time_ms / 1000))))
    deploy_data = {
        'jobs': [j.data for j in job_datas],
        'title': title,
        'colors': builds.COLORS,
        'deployStartTimeMs': deploy_start_time_ms,
        'deployEndTimeMs': deploy_end_time_ms,
    }
    return r"""
<html>
<head>
<script>
    var data = %s;
</script>
<style>
  .flamechart { display: table; width: 100%%; }
  .tr { display: table-row; width: 100%%; }
  .label {
    display: table-cell;
    vertical-align: middle;
    white-space: nowrap;
    font-size: 10pt;
    font-family: sans-serif;
    padding-right: 0.5em;       /* SPACE BETWEEN TEXT AND BAR */
  }
  .bar-container {
     display: table-cell;
     width: 100%%;
}
  .bar {
    height: 12pt;               /* WIDTH (WELL, HEIGHT) OF THE BAR */
    width: 100%%;
    margin: 2pt auto 2pt auto;  /* SPACING BETWEEN ROWS */
    position: relative;
    display: block;
  }
  .interval {
    float: left;
    height: 100%%;
    width: 100%%;
  }

  /* For the x axis */
  .lastrow {
    border-top: 1pt solid #BBBBBB;
    height: 6pt;
  }
  .x-axis {
    display: table-cell;
    width: 100%%;
  }
  .axis-label {
    text-align: center;
    height: 12pt;
    width: 100%%;
    padding-top: 2pt;
    font-size: 10pt;
    font-family: sans-serif;
    float: left;
    position: relative;
    display: block;
  }

  /* From https://www.w3schools.com/css/css_tooltip.asp */
  .interval .tooltip {
    visibility: hidden;
    background-color: black;
    color: #fff;
    text-align: center;
    font-size: 8pt;
    font-family: sans-serif;
    padding: 5px 10px;
    border-radius: 6px;
    /* Position the tooltip text above the div */
    position: absolute;
    bottom: 110%%;
    left: 30%%;
    z-index: 1;
  }
  .interval:hover .tooltip { visibility: visible; }

  /* Toggle the little triangle whenever a row is collapsed; based on
   * https://www.digitalocean.com/community/tutorials/css-collapsible */
  .toggle { display: none; }
  .lbl-toggle .label::before {
    font-size: 10pt;
    content: "\25BE";
    vertical-align: middle;
    padding-right: 3px;
  }
  .toggle:checked + .lbl-toggle .label::before {
    content: "\25B8";
  }
</style>
</head>
<body>
<!-- The content for the h1 and the flamechart are filled in by javascript -->
<h1 id="title">Rendering...</h1>
<div id="flamechart" class="flamechart">
   ...
</div>

<script>
var safe = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
                 .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
                 .replace(/'/g, "&#039;");

// Javascript to hide all nodes (rows) that are children of a given node.
function toggleCollapse(id) {
    [...document.getElementsByClassName(`childOf${id}`)].forEach(
        node => node.classList.toggle(`collapse${id}`)
    );
}

// TODO(csilvers): figure out how to merge the job-datas.
var nodes = [];

// A node is a bar-graph bar.  We render them in DFS order.
// But of course a table renders rows in linear order.  We
// linearize here, but keep track of the tree structure via
// two new fields we add to the data: parentIDs and hasChildren.
function flattenNodes(node, parentIDs) {
   var myID = nodes.length;  // any unique integer will do
   nodes = [
       ...nodes,
       {...node, id: myID, parentIDs, hasChildren: !!node.children.length}
   ];
   node.children.forEach(c => flattenNodes(c, [...parentIDs, myID]));
}

data.jobs.forEach(job => flattenNodes(job.nodeRoot, []));  // populates `nodes`

var deployTimeMs = data.deployEndTimeMs - data.deployStartTimeMs;

// We want each bar to look like this:
//   <div class="tr">
//     <div class="label">e2e-worker-1</div>
//     <div class="bar-container">
//       <div class="bar">
//         <div class="interval c1" style="max-width:10.5%%">
//           <div class="tooltip">WAITING: 0 - 6.5</div>
//         </div>
//         <div class="interval c2" style="max-width:89.5%%">
//           <div class="tooltip">RUNNING: 6.5 - 60</div>
//         </div>
//       </div>
//     </div>
//   </div>
// We create all this html in one go and use innerHTML to insert it
// into the html proper.
var html = [];
nodes.forEach(node => {
    var id = node.id;
    // Add a style that children can use to collapse this node
    document.styleSheets[0].insertRule(`.collapse${id} {visibility:collapse}`);

    // Add some unstyled classes that we can use to look up an entire subtree
    // via document.getElementsByClassName("childOfX").
    html.push(
        `<div class="tr ${node.parentIDs.map(i => `childOf${i}`).join(" ")}">`
    );

   // Add the bar's label, with a "collapse triangle" if appropriate.
   var indent = node.parentIDs.length + 1;
    if (node.hasChildren) {
        html.push(`<input id="collapsible${id}" class="toggle" ` +
                  `type="checkbox" ` +
                  `onclick="javascript:toggleCollapse(${id})">`);
        html.push(`<label for="collapsible${id}" class="lbl-toggle">`);
    }
    html.push(`<div class="label" style="padding-left: ${indent}em">`);
    html.push(`${safe(node.name)}`);
    html.push(`</div>`);
    if (node.hasChildren) {
        html.push(`</label>`);
    }

    // Add the bar!
    html.push(`<div class="bar-container">`);
    html.push(`<div class="bar">`);
    // So all our jobs line up on the x-axis, we insert "fake"
    // intervals from deploy-start-time to job-start-time,
    // and from job-end-time to deploy-end-time.
    var preJobInterval = {
        startTimeMs: data.deployStartTimeMs,
        endTimeMs: node.intervals[0].startTimeMs,
        timeRangeRelativeToJobStart: "",
        mode: "[job not started]",
        colorIndex: "transparent",
    };
    var intervals = [preJobInterval, ...node.intervals];
    intervals.forEach(interval => {
        // TODO(csilvers): adjust pct when rows are collapsed.
        var pct = ((interval.endTimeMs - interval.startTimeMs) * 100
                   / deployTimeMs);
        html.push(`<div class="interval c${interval.colorIndex}" ` +
                  `style="max-width:${pct}%%">`);
        html.push(`<div class="tooltip">${safe(interval.mode)}: ` +
                  `${interval.timeRangeRelativeToJobStart}</div>`);
        html.push(`</div>`);
    });
    html.push(`</div>`);
    html.push(`</div>`);
    html.push(`</div>`);
});

// Insert the CSS for the colors.  The white color we actually want to
// be transparent (so grid-marks show up on it), so we handle that
// case specially.  In addition there's a style to be *explicitly* transparent.
data.colors.forEach((c, i) => {
    if (c.match(/#ffffff/i)) {
        document.styleSheets[0].insertRule(`.c${i} { visibility: hidden; }`)
    } else {
        document.styleSheets[0].insertRule(`.c${i} { background: ${c}; }`)
    }
});
document.styleSheets[0].insertRule(`.ctransparent { visibility: hidden; }`);

// Insert the CSS for the grid marks.  We need to do this dynamically
// because we want them every 60 seconds, and we need to know he
// width of the graph (in seconds) to do that.
var tickIntervalMs = 60 * 1000;
var numTicks = deployTimeMs / tickIntervalMs;
// Let's make sure we don't have too many ticks.  20 seems a good maximum.
while (numTicks > 20) {
   tickIntervalMs += 60 * 1000;
   var numTicks = deployTimeMs / tickIntervalMs;
}
var tickGapPct = 100.0 / numTicks;
document.styleSheets[0].insertRule(`.bar-container {
  background-size: ${tickGapPct}%% 100%%;
  background-image: linear-gradient(to right, #BBBBBB 1px, transparent 1px);
}`);
// And insert a row of html to serve as the x-axis.  We center
// each number under the grid by putting it in a span that is
// centered under the grid-mark.  This doesn't necessarily work
// for the last grid-mark, which might not have enough space after
// it for the span, so we leave that as a TODO.
// First, let's continue the grid-marks down below the graph a teeny bit.
html.push(`<div class="tr">`);
html.push(`<div class="label lastrow"></div>`);
html.push(`<div class="bar-container lastrow"></div>`);
html.push(`</div>`);

html.push(`<div class="tr">`);
html.push(`<div class="label"></div>`);
html.push(`<div class="x-axis">`);
html.push(`<div class="axis-label" style="max-width: ${tickGapPct/2}%%">` +
          `</div>`);  // initial padding so that centering works right
for (var i = 1; i < numTicks - 1; i++) {   // TODO(csilvers): handle last tick
    html.push(`<div class="axis-label" style="max-width: ${tickGapPct}%%">`);
    html.push(`${Math.round(i * tickIntervalMs / 60000)}m`);
    html.push(`</div>`);
}
html.push(`</div>`);
html.push(`</div>`);

document.getElementById('title').innerHTML = safe(data.title);
document.getElementById('flamechart').innerHTML = html.join("\n");
</script>
</body>
</html>
""" % json.dumps(deploy_data, sort_keys=True, indent=2)


def _fetch_build(job, build_id, output_dir, jenkins_client, force=False):
    """Download, save, and return the data-needed-to-render for one job."""
    mkdir_p(output_dir)
    outfile = os.path.join(
        output_dir, '%s:%s.data' % (job.replace('/', '--'), build_id))

    if not force and os.path.exists(outfile):
        with open(outfile, 'rb') as f:
            step_html = f.read().decode('utf-8')
        m = re.search(r'<script>var parameters = (.*?)</script>',
                      step_html)
        job_params = json.loads(m.group(1) if m else '{}')
        # We get the job-start time by the file's mtime.
        job_start_time = os.path.getmtime(outfile)
        return (step_html, job_params, job_start_time, outfile)

    try:
        job_params = jenkins_client.fetch_job_parameters(job, build_id)
        step_html = jenkins_client.fetch_pipeline_steps(job, build_id)
        step_root = steps.parse_pipeline_steps(step_html)
        if not step_root:
            raise DataError(job, build_id, "invalid job? (no steps found)")
        job_start_time = jenkins_client.fetch_job_start_time(
            job, build_id, step_root)
    except jenkins.HTTPError as e:
        raise DataError(job, build_id, "HTTP error: %s" % e)

    with open(outfile, 'wb') as f:
        f.write(step_html.encode('utf-8'))
        params_text = ('\n\n<script>var parameters = %s</script>'
                       % json.dumps(job_params))
        f.write(params_text.encode('utf-8'))
    # Set the last-modified time of this file to its start-time.
    os.utime(outfile, (job_start_time, job_start_time))

    return (step_html, job_params, job_start_time, outfile)


def _download_one_build(param):
    # (The weird parameter format is because this is used by Pool().)
    (job, build_id, output_dir, jenkins, force) = param
    print("Fetching %s:%s" % (job, build_id))
    try:
        (_, job_params, job_start_time, outfile) = _fetch_build(
            job, build_id, output_dir, jenkins, force)
    except DataError as e:
        print("ERROR: skipping %s:%s: %s" % (e.job, e.build_id, e))
        return

    # Now create a symlink organized by date and title.
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


def download_builds(builds, output_dir, jenkins_username, jenkins_password,
                    force=False):
    """Download and save the data-needed-to-render for all jobs.

    We ask jenkins what builds it knows about for the given jobs,
    then download them all to get a `.data` file that is suitable
    for passing as input to this script (at some later date) to
    graph this build.

    Arguments:
        builds: a list of either builds or jobs, e..g
            ["deploy/build-webapp", "deploy/webapp-test:1214"]
        For builds where the build-id is omitted, we fetch all
        build-ids for the given job.
        output_dir: the directory to put all the data files
        jenkins_username, jenkins_password: a valid API token
        force: if False, don't fetch any jobs that already have a
               data-file in output_dir.  If True, fetch everything.
    """
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


def main(buildses, output_dir, jenkins_username=None, jenkins_password=None):
    """jenkins_* vars are not needed if all builds are .data files."""
    job_datas = []
    for build in buildses:
        if build.endswith('.data'):  # Used a cached file to avoid the network
            (job, build_id) = os.path.basename(build[:-len('.data')]).replace(
                '--', '/').split(':')
            with open(build, 'rb') as f:
                step_html = f.read().decode('utf-8')
            m = re.search(r'<script>var parameters = (.*?)</script>',
                          step_html)
            job_params = json.loads(m.group(1) if m else '{}')
            # We get the job-start time by the file's mtime.
            job_start_time = os.path.getmtime(build)
            outfile = build
        else:
            if jenkins_password:
                jenkins_client = jenkins.get_client_via_password(
                    jenkins_username, jenkins_password)
            else:
                jenkins_client = jenkins.get_client_via_keeper(
                    KEEPER_RECORD_ID)
            (job, build_id) = build.split(':')
            (step_html, job_params, job_start_time, outfile) = _fetch_build(
                job, build_id, output_dir, jenkins_client)

        step_root = steps.parse_pipeline_steps(step_html)
        node_root = nodes.steps_to_nodes(step_root)
        job_datas.append(builds.BuildData(
            job, build_id, job_start_time, job_params, node_root))

    job_datas.sort(key=lambda jd: jd.job_start_time_ms)

    html_file = outfile.replace('.data', '.html')
    html = create_html(job_datas)
    with open(html_file, 'wb') as f:
        f.write(html.encode('utf-8'))
    webbrowser.open(html_file)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'build', nargs='+',
        help=("Jenkins builds to fetch, e.g. deploy/build-webapp:1543 "
              "OR a json-filename like deploy-build-webapp:1543.json."))
    parser.add_argument('--jenkins-username',
                        default='jenkins@khanacademy.org')
    parser.add_argument('--jenkins-pw',
                        help=('API token that gives access to job data. '
                              'If not set, fetch the secret from keeper '
                              '(record %s)' % KEEPER_RECORD_ID))
    parser.add_argument('-d', '--output-dir',
                        default='/tmp/jenkins-job-perf-analysis',
                        help='Directory to write the flamechart output file')
    parser.add_argument('--fetch-only', action='store_true',
                        help=('Only fetch the jenkins data, but do not '
                              'create a graph.  In this mode, the BUILD '
                              'arguments can be just a job-name, in which '
                              'case we download all builds for that job.'))

    args = parser.parse_args()

    try:
        if args.fetch_only:
            download_builds(args.build, args.output_dir,
                            args.jenkins_username, args.jenkins_pw)
        else:
            main(args.build, args.output_dir,
                 args.jenkins_username, args.jenkins_pw)
    except Exception:
        import pdb
        import sys
        import traceback
        extype, value, tb = sys.exc_info()
        traceback.print_exc()
        pdb.post_mortem(tb)
