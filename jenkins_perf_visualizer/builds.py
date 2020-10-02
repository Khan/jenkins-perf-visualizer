"""Holds information about a single Jenkins build.

A "build" is a single execution of a jenkins job.  So if you have
a job called "deploy", then a build would be something like "deploy:5"
(where 5 is the "build id").

This holds all our (parsed) information about a single Jenkins build.
It includes all the timing information about the build as a tree of
Node objects.  It also includes other information that will be useful
for visualizing.

This is also the file that defines the colorization of bars of the
visualization graph.  TODO(csilvers): redo how we do colors.

The public api here is the BuildData class.
"""
import re
import time

import nodes


# A map from regexp matching a node-name, to colors to use for our output bars.
# We have 3 different versions of each color: light, medium, and dark.
# Light is used when the node is waiting for an executor (gce machine),
# medium when sleeping, and dark when running.
# The keys are for names of stages() and parallel() branches, as
# described in the top-of-file docstrings.
# If a job has a node-name thats not listed below, it will be colored
# black.  These rgb values come from, e.g.
#    https://www.rapidtables.com/web/color/red-color.html
# TODO(csilvers): have this only takes regexp keys, and require they match
# the whole string.
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
    nodes.RUNNING: 1.0,
    nodes.SLEEPING: 0.6,
    nodes.WAITING: 0.6,
    nodes.AWAITING_EXECUTOR: 0.3,
    nodes.NOT_RUNNING: 0.0,  # white
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
COLORS = ['#000000'] + sorted(set(_COLOR_MAP.values()))


class BuildData(object):
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
            'colors': COLORS,
            'nodeRoot': node_to_json(node_root),
        }

    _COLOR_TO_INDEX = {c: i for (i, c) in enumerate(COLORS)}

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
