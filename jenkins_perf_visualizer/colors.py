"""Routines for handling bar colors."""

from jenkins_perf_visualizer import nodes


# How saturated to make the bar-color, based on the node mode.
# This is an "alpha" value (assuming a white background) from 0-1.
_NODE_SATURATION = {
    nodes.RUNNING: 1.0,
    nodes.SLEEPING: 0.6,
    nodes.WAITING: 0.6,
    nodes.AWAITING_EXECUTOR: 0.3,
    nodes.NOT_RUNNING: 0.0,  # white
}


def color_map(config):
    """A map from (name_regexp, mode) to #rrggbb.

    Mode is nodes.RUNNING/etc.  We take the colors from the config
    and combine them with the saturaations above to get the full map.
    """
    retval = {}
    for (name_regexp, color) in config.get('colors', {}).items():
        for (mode, alpha) in _NODE_SATURATION.items():
            color_with_alpha = '#%02x%02x%02x' % (
                int(int(color[1:3], 16) * alpha + 255 * (1 - alpha)),
                int(int(color[3:5], 16) * alpha + 255 * (1 - alpha)),
                int(int(color[5:7], 16) * alpha + 255 * (1 - alpha)))
            retval[(name_regexp, mode)] = color_with_alpha
    return retval


def color_to_id(config):
    """A map from color to a unique id for that color.

    (We use black for names that don't match any regexp in the color-map.)
    """
    colors = ['#000000'] + sorted(set(color_map(config).values()))
    return {c: i for (i, c) in enumerate(colors)}
