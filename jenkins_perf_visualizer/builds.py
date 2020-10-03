"""Holds information about a single Jenkins build.

A "build" is a single execution of a jenkins job.  So if you have
a job called "deploy", then a build would be something like "deploy:5"
(where 5 is the "build id").

This holds all our (parsed) information about a single Jenkins build.
It includes all the timing information about the build as a tree of
Node objects.  It also includes other information that will be useful
for visualizing.

The public api here is the BuildData class.
"""
import time

from jenkins_perf_visualizer import colors


class BuildData(object):
    """All the data needed to graph nodes for a single jenkins build.

    The main data is in "nodes", which is a list of time-ranges.
    The time-range values are floating-point time_t's.
    They also have a mode -- RUNNING, etc -- and a color index
    into `colors`, which is a list of elements like "#RRGGBB".
    """
    def __init__(self, config, job, build_id,
                 build_start_time, build_params, node_root):
        self.build_start_time_ms = build_start_time * 1000  # as a time-t
        pretty_name = '<%s:%s>' % (job, build_id)

        self.color_map = colors.color_map(config)
        self.color_to_id = colors.color_to_id(config)

        _time = lambda ms: (
            time.localtime((self.build_start_time_ms + ms) / 1000.0))

        def node_to_json(node):
            node_name = node.name or pretty_name
            return {
                'name': node_name,
                'children': [node_to_json(c) for c in node.children],
                'intervals': [{
                    'startTimeMs': t.start_ms + self.build_start_time_ms,
                    'endTimeMs': t.end_ms + self.build_start_time_ms,
                    'timeRangeRelativeToBuildStart': (
                        "%s - %s (%.2fs)"
                        % (time.strftime("%Y/%m/%d:%H:%M:%S",
                                         _time(t.start_ms)),
                           time.strftime("%H:%M:%S",
                                         _time(t.end_ms)),
                           (t.end_ms - t.start_ms) / 1000.0)),
                    'mode': t.mode,
                    'colorId': self._color_id(node_name, t.mode),
                } for t in node.intervals
                ],
            }

        def max_end_time(node):
            end_time = max([t.end_ms for t in node.intervals])
            return max([end_time] + [max_end_time(c) for c in node.children])

        self.data = {
            'jobName': job,
            'buildId': build_id,
            'title': build_params.get(config.get("titleParameter"), "Build"),
            'parameters': build_params,  # not used; for help in debugging
            'buildStartTimeMs': self.build_start_time_ms,
            'buildEndTimeMs': (self.build_start_time_ms +
                               max_end_time(node_root)),
            'nodeRoot': node_to_json(node_root),
        }

    def _color_id(self, node_name, mode):
        """Determine what color to use for a single bar of our graph."""
        for ((mapname, mapmode), color) in self.color_map.items():
            if mapname.match(node_name) and mapmode == mode:
                break
        else:
            color = '#000000'

        return self.color_to_id[color]
