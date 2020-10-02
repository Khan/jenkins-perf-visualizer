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
"""
from __future__ import absolute_import

import errno
import json
import multiprocessing.pool
import os
import re
import subprocess
import time
import webbrowser

import jenkins


# TODO(csilvers): move to a config file
KEEPER_RECORD_ID = 'mHbUyJXAmnZyqLY3pMUmjQ'


# The modes that a job can be in.
RUNNING = "RUNNING"
SLEEPING = "Sleeping"  # running sleep()
WAITING = "Waiting"   # running waitFor() or prompt()
AWAITING_EXECUTOR = "Awaiting executor"   # waiting for a new  machine
NOT_RUNNING = "[not running]"


# A map from regexp matching a node-name, to colors to use for our output bars.
# We have 3 different versions of each color: light, medium, and dark.
# Light is used when the node is waiting for an executor (gce machine),
# medium when sleeping, and dark when running.
# The keys are for names of stages() and parallel() branches, as
# described in the top-of-file docstrings.
# If a job has a node-name thats not listed below, it will be colored
# black.  These rgb values come from, e.g.
#    https://www.rapidtables.com/web/color/red-color.html
_NODE_COLORS = {
    # Used for steps outside any step() or parallel().
    None: "b22222",  # red

    # All jobs (stages run via notify.groovy and other helper functions)
    'main': "b22222",  # red
    '_watchdog': "a9a9a9",  # gray
    'Resolving commit': "006400",  # green
    'Talking to buildmaster': "006400",  # green

    # 2ndsmoketest-priming
    'Priming': "daa520",  # gold

    # build-webapp
    'Merging in master': "006400",  # green
    'Deploying': "daa520",  # gold
    re.compile(r'^deploy-'): "00008b",  # blue
    'Send changelog': "006400",  # green

    # deploy-webapp
    'Await first smoke test and set-default confirmation': "a9a9a9",  # gray
    'Promoting and monitoring': "daa520",  # gold
    'monitor': "00008b",  # blue
    'promote': "00008b",  # blue
    'wait-and-start-tests': "00008b",  # blue
    'Await finish-up confirmation': "a9a9a9",  # gray
    'Merging to master': "006400",  # green

    # merge-branches
    # <none needed>

    # webapp-test
    'Determining splits & running tests': "daa520",  # gold
    'Running tests': "006400",  # green
    'determine-splits': "006400",  # green
    'Analyzing results': "006400",  # green
    re.compile(r'^test-'): "00008b",  # blue

    # e2e-test
    re.compile(r'^e2e-test-'): "00008b",  # blue
    re.compile(r'^job-'): "daa520",  # gold
}


# How saturated to make the bar-color, based on the node mode.
# This is an "alpha" value (assuming a white background) from 0-1.
_NODE_SATURATION = {
    RUNNING: 1.0,
    SLEEPING: 0.6,
    WAITING: 0.6,
    AWAITING_EXECUTOR: 0.3,
    NOT_RUNNING: 0.0,  # white
}


# Combine the colors and the saturation/alpha to get all the colors.
_COLOR_MAP = {
    (name, mode): '#%02x%02x%02x' % (
        int(int(color[:2], 16) * alpha + 255 * (1 - alpha)),
        int(int(color[2:4], 16) * alpha + 255 * (1 - alpha)),
        int(int(color[4:], 16) * alpha + 255 * (1 - alpha)))
    for (name, color) in _NODE_COLORS.items()
    for (mode, alpha) in _NODE_SATURATION.items()
}
_COLORS = ['#000000'] + sorted(set(_COLOR_MAP.values()))


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


class Step(object):
    """Important (to us) information about one executed pipeline "step".

    A "step" is a single jenkins pipeline command: `parallel()`, `sh()`,
    etc.  It's also a single row in the flow-pipeline page.
    """
    def __init__(self, id, indentation, step_text, previous_steps):
        """Populates all the fields we can from a given 'step' html.

        "step_text" is the stuff between the <a> and </a> in this:
           <td tooltip="ID: 4" style="padding-left: 25px">\n<a href="/job/deploy/job/e2e-test/16585/execution/node/4/">\nAllocate node : Start - (16 min in block)</a>\n</td>\n  # NoQA:L501
        """
        # An integer id for the node, given by jenkins.
        self.id = int(id)

        # How much we are indented in html, used to infer the tree structure.
        self.indent = int(indentation)

        # The parent-node, as an Step, and the children, likewise.
        self.children = []
        self.parent = self._parent(previous_steps)
        if self.parent:
            self.parent.children.append(self)

        # True if we are a waitUntil(), prompt(), or sleep().
        self.is_waiting = ('Wait for condition : ' in step_text or
                           'Wait for interactive input' in step_text)
        self.is_sleeping = 'Sleep - ' in step_text

        # True if we are allocating a new node (on a jenkins worker, say).
        self.is_new_worker = 'Allocate node : Start' in step_text
        # True if we start a new stage (via the stage() pipeline command).
        self.is_new_stage = 'Stage : Start' in step_text
        # True if our children are executed in parallel.
        self.is_parallel_parent = 'Execute in parallel :' in step_text
        # True if we are starting a new branch inside a parallel().
        self.is_branch_step = 'Branch: ' in step_text

        # The node name, e.g. 'determine-splits'.
        # If we don't have one, we inherit from our parent.
        self.name = self._name(step_text)

        # How long we ran for.
        self.elapsed_time_ms = self._elapsed_time(step_text)

        # When we ran.  Our start time is determined by "dead
        # reckoning" -- when our parent started, plus however long all
        # our prior sibling nodes ran for.  (Unless the parent was an
        # "execute in parallel", in which case we ignore the siblings.)
        # This implies that the root node started at time 0.
        self.start_time_ms = self._start_time()

    INDENT_RE = re.compile(r'\bpadding-left:\s*([\d]+)', re.I)
    BRANCH_RE = re.compile(r'\bBranch: (\S+) - ')
    ELAPSED_TIME_RE = re.compile(
        r'(?:([\d.]+) min )?'
        r'(?:([\d.]+) sec )?'
        r'(?:([\d.]+) ms )?'
        r'in (block|self)')

    def has_new_name(self):
        """True if our name diffs from our parent's."""
        return not self.parent or self.name != self.parent.name

    def _parent(self, previous_steps):
        """Find the parent node based on indentation.

        Basically, we look at all nodes backwards from ours, until
        we find one whose indentation is less than ours.  If our
        indentation is 0, then we have no parent.
        """
        for candidate_parent in previous_steps[::-1]:
            if candidate_parent and candidate_parent.indent < self.indent:
                return candidate_parent
        return None

    def _name(self, step_text):
        # We start a new name in the following situations:
        # 1. We are starting a named branch (of a parallel() step)
        # 2. We are starting a new stage (via a stage() step)
        # Otherwise, we inherit the name from our parent.
        if self.is_branch_step:
            m = self.BRANCH_RE.search(step_text)
            return m.group(1)
        elif self.parent and self.parent.is_new_stage:
            return step_text.split(' - ')[0]  # our text is the stage-name
        elif self.parent:
            return self.parent.name
        else:
            return None

    def _elapsed_time(self, step_text):
        # The text will say "a.b sec in block" or "a.b sec in self",
        # or "a.b min c.d sec in block/self", or "a ms in self/block"
        #
        # NOTE: due to a bug in jenkins the elapsed time is wrong
        # for "Branch:" steps (which should be treated like blocks
        # but aren't).  We can't fix that until we know all our
        # children, so we fix it up manually below.
        m = self.ELAPSED_TIME_RE.search(step_text)
        time = float(m.group(1) or 0) * 60000  # min
        time += float(m.group(2) or 0) * 1000  # sec
        time += float(m.group(3) or 0)         # ms
        return time

    def _start_time(self):
        if self.parent is None:
            return 0
        if self.parent.is_parallel_parent:
            # The "parallel" node just holds a bunch of children,
            # all of which start at the same time as it.
            return self.parent.start_time_ms
        if self.parent.is_new_worker:
            # We have to deal with a special case: if our parent was an
            # "allocate node" step, then there's no pipeline step for how log
            # it spent waiting for an executor to come online, which means our
            # start time doesn't account for that waiting time.  Luckily we and
            # our parent always have the same end-time, by construction, so we
            # can figure out our start-time that way.
            return (self.parent.start_time_ms +
                    self.parent.elapsed_time_ms - self.elapsed_time_ms)

        # Our start-time is our parent's start time, plus however
        # long it took all our prior siblings to run.
        start_time = self.parent.start_time_ms
        start_time += sum(sib.elapsed_time_ms for sib in self.parent.children
                          if sib != self)
        return start_time


def parse_pipeline_steps(html):
    """Parse the pipeline-steps html page to get an actual execution tree."""
    # The html here has a very regular structure.  Steps look like:
    #   <td tooltip="ID: XX" style="padding-left: YYpx"><a href=...>ZZ</a></td>
    rows = re.findall(
        (r'<td tooltip="ID: (\d+)" style="padding-left: (\d+)px">'
         r'<a href=[^>]*>([^<]*)</a></td>'),
        html
    )

    steps = []
    for (id, indentation, step_text) in rows:
        # Unescape step_text, lamely.  TODO(csilvers): use a real parser.
        step_text = step_text. \
            replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
        step = Step(id, indentation, step_text.strip(), steps)
        steps.append(step)

    # Now patch over the elapsed-time bug for "Branch:" nodes.
    # (See the docstring for Step._elapsed_time(), above.)
    for step in steps:
        if step.is_branch_step:
            step.elapsed_time_ms = sum(c.elapsed_time_ms
                                       for c in step.children)

    return steps[0] if steps else None


class Node(object):
    """Holds information about what one node is doing.

    This coalesces the steps from the Step data structure into a small
    number of datapoints that's useful for visualizing.  Every time we
    run a parallel() step, or start a new stage(), or start doing work
    on another worker machine, we create a new Node.  For simple Node's
    we just store the start-time and end-time.  But we keep track of
    when the Node is sleeping or waiting for something (e.g. clicking on
    the "set default" button), since that's probably useful info for
    visualization.
    """
    def __init__(self, step):
        self.name = step.name

        # Child-nodes.  Each node represents a subtree of the steps tree.
        # Node A is a child of node B if node a's subtree-root is a
        # child of some step in node B.
        self.children = []

        # A set of intervals.  Each interval in this time-range is a triple:
        #   (start_time_ms, end_time_ms, mode)
        # where "mode" is one of RUNNING, SLEEPING, etc.,
        # as described in the top-of-file docsring.
        self.timerange = []

    def add_step(self, step):
        """Add the step and its children to our timerange."""
        assert step.has_new_name()
        self._add_timerange(step, RUNNING)
        for child in step.children:
            self._recursive_add_timerange(child)

    @staticmethod
    def _sort_nodes(node):
        """Sort alphabetically, but numerically for nodes like "e2e-node-1"."""
        if not node.name:
            return None
        retval = [node.timerange[0][0]]   # start-time of the node
        parts = node.name.split('-')
        retval.extend([int(p) if p.isdigit() else p for p in parts])
        return retval

    def add_child(self, child_node):
        if child_node.name not in [c.name for c in self.children]:
            self.children.append(child_node)
            # We could just insert in sorted order, but whatever.
            self.children.sort(key=self._sort_nodes)

    def _add_timerange(self, step, mode):
        start = step.start_time_ms
        end = start + step.elapsed_time_ms
        self.timerange.append((start, end, mode))

    def _recursive_add_timerange(self, step):
        """Add time-ranges for "interesting" children in the same node.

        We do a DFS traversal of the step-tree starting at the input node,
        and every time we see a sleep or waitFor node we make a note of
        it.  The idea is to give more detail as to what this node is
        spending its time on.
        """
        # We only consider work done by *our* node.
        if step.has_new_name():
            return

        if step.is_sleeping:
            self._add_timerange(step, SLEEPING)
        elif step.is_waiting:
            self._add_timerange(step, WAITING)
        elif step.is_new_worker:
            # Every "allocate node" should have one child, which is
            # "begin node".  The time between allocate-node and begin-node
            # is time waiting for an executor (new gce machine) to start.
            assert len(step.children) == 1
            start_step = step.children[0]
            # We can't call _add_timerange because our timerange here
            # doesn't correspond to an entire step.
            self.timerange.append((step.start_time_ms,
                                   start_step.start_time_ms,
                                   AWAITING_EXECUTOR))

        for child in step.children:
            self._recursive_add_timerange(child)

    def normalize_timeranges(self):
        """Resolve overlaps in our time-ranges by splitting them."""
        # Partially overlapping ranges aren't meaningful to us: a child
        # step should be entirely inside its parent, and siblings steps
        # should not overlap, e.g. if we have two ranges, A-B and C-D,
        # they should be time-sorted like A-C-D-B or A-B-C-D, but not
        # A-C-B-D.  Let's just assert that's the case.
        # Note however we don't have great resolution on our timestamps
        # so we allow up to a second of overlap for measurement error.
        for (start_x, end_x, _) in self.timerange:
            for (start_y, end_y, _) in self.timerange:
                if start_x < start_y:  # we get the othre half via symmetry
                    assert end_x - 60000 <= start_y or end_x + 60000 >= end_y

        # Sort to preserve nestedness: so by start-time ASC and end-time DESC.
        self.timerange.sort(key=lambda startend: (startend[0], -startend[1]))

        if_nonzero = lambda start, end, mode: (
            [(start, end, mode)] if start < end else [])

        # This is quadratic time, but the linear-time algorithm is
        # pretty hard to follow, and our N is going to be small anyway.
        new_timerange = []
        for (start, end, mode) in self.timerange:
            # If our start is greater than last-entry's end, we go after
            # all existing ranges and can just append.  Let's put
            # in a not-running too between them.
            largest_end = new_timerange[-1][1] if new_timerange else 0
            if start >= largest_end:
                new_timerange.extend(
                    if_nonzero(largest_end, start, NOT_RUNNING) +
                    if_nonzero(start, end, mode)
                )
                continue

            # Otherwise we nest inside some range.  Find it by finding the
            # existing range with the largest start <= ours.
            i = len(new_timerange) - 1
            while new_timerange[i][0] > start:
                i -= 1
            (old_start, old_end, old_mode) = new_timerange[i]
            new_timerange[i:i + 1] = (
                if_nonzero(old_start, start, old_mode) +
                if_nonzero(start, end, mode) +
                if_nonzero(end, old_end, old_mode)
            )

        # Finally, as we copy back over, clean things up by merging adjacent
        # ranges that share the same mode.
        self.timerange = [new_timerange[0]]
        for (start, end, mode) in new_timerange[1:]:
            if (self.timerange[-1][1], self.timerange[-1][2]) == (start, mode):
                self.timerange[-1] = (self.timerange[-1][0], end, mode)
            else:
                self.timerange.append((start, end, mode))


def _steps_to_nodes(step_root, current_node, name_to_node):
    if step_root.has_new_name():
        name_to_node.setdefault(step_root.name, Node(step_root))
        name_to_node[step_root.name].add_step(step_root)
        if current_node:
            current_node.add_child(name_to_node[step_root.name])
        current_node = name_to_node[step_root.name]

    for child in step_root.children:
        _steps_to_nodes(child, current_node, name_to_node)


def steps_to_nodes(step_root):
    """Convert a list of Steps into a list of Nodes."""
    name_to_node = {}
    _steps_to_nodes(step_root, None, name_to_node)

    for node in name_to_node.values():
        node.normalize_timeranges()

    return name_to_node[None]  # the root node is the one with no name


class JobData(object):
    """All the data needed to graph nodes for a single jenkins job.

    The main data is in "nodes", which is a list of time-ranges.
    The time-range values are floating-point time_t's.
    They also have a mode -- RUNNING, etc -- and a color index
    into `colors`, which is a list of elements like "#RRGGBB".
    """
    def __init__(self, job, build_id, job_start_time, job_params, node_root):
        self.job_start_time_ms = job_start_time * 1000  # as a time-t
        pretty_name = '<%s:%s>' % (job, build_id)

        _time = lambda ms: (
            time.localtime((self.job_start_time_ms + ms) / 1000.0))

        def node_to_json(node):
            return {
                'name': node.name or pretty_name,
                'children': [node_to_json(c) for c in node.children],
                'intervals': [{
                    'startTimeMs': t[0] + self.job_start_time_ms,
                    'endTimeMs': t[1] + self.job_start_time_ms,
                    'timeRangeRelativeToJobStart': (
                        "%s - %s (%.2fs)"
                        % (time.strftime("%Y/%m/%d:%H:%M:%S", _time(t[0])),
                           time.strftime("%H:%M:%S", _time(t[1])),
                           (t[1] - t[0]) / 1000.0)),
                    'mode': t[2],
                    'colorIndex': self._color_index(node.name, t[2]),
                } for t in node.timerange
                ],
            }

        def max_end_time(node):
            end_time = max([t[1] for t in node.timerange])
            return max([end_time] + [max_end_time(c) for c in node.children])

        self.data = {
            'jobName': job,
            'buildId': build_id,
            'title': job_params.get('REVISION_DESCRIPTION', '<unknown job>'),
            'parameters': job_params,  # not used; for help in debugging
            'jobStartTimeMs': self.job_start_time_ms,
            'jobEndTimeMs': self.job_start_time_ms + max_end_time(node_root),
            'colors': _COLORS,
            'nodeRoot': node_to_json(node_root),
        }

    _COLOR_TO_INDEX = {c: i for (i, c) in enumerate(_COLORS)}

    def _color_index(self, node_name, mode):
        """Determine what color to use for a single bar of our graph."""
        color = _COLOR_MAP.get((node_name, mode), None)
        if color is None:  # maybe because we match a regexp color-key
            for ((mapname, mapmode), color) in _COLOR_MAP.items():
                if (hasattr(mapname, 'search') and mapname.search(node_name)
                        and mapmode == mode):
                    break
            else:
                color = '#000000'

        return self._COLOR_TO_INDEX[color]


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
        'colors': _COLORS,
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
        step_root = parse_pipeline_steps(step_html)
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


def main(builds, output_dir, jenkins_username=None, jenkins_password=None):
    """jenkins_* vars are not needed if all builds are .data files."""
    job_datas = []
    for build in builds:
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

        step_root = parse_pipeline_steps(step_html)
        node_root = steps_to_nodes(step_root)
        job_datas.append(JobData(job, build_id, job_start_time, job_params,
                                 node_root))

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
