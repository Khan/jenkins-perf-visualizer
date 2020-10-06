"""Code for reading the config json file.

load() is the public API, though scripts will use add_*_arg() as well.
"""
import argparse
import json
import os
import re

# These are the argparse arguments that a script can include to
# override defaults in the config.json file.  In each, `parser`
# is an argparse.ArgumentParser oject.


def add_config_arg(parser):
    parser.add_argument(
        '--config', '-c', default="jenkins-perf.json",
        help=("The name of a json config file to use.  Any other optional "
              "parameters will override the values read from here."))


def add_datadir_arg(parser):
    parser.add_argument(
        '--datadir', '-d', default=argparse.SUPPRESS,
        help=("Directory to write the output data files, overriding the "
              "value in the config.json file."))


def add_download_threads_arg(parser):
    parser.add_argument(
        '--download-threads', type=int, default=argparse.SUPPRESS,
        help=("How many threads to use when downloading, overriding the "
              "value in the config.json file"))

# TODO(csilvers): flesh these out.


def _read(config_filename):
    """Read a comment-augmented json file from the given filename."""
    with open(config_filename) as f:
        lines = f.readlines()
    content_lines = [l for l in lines if not l.lstrip().startswith('//')]
    content = ''.join(content_lines)
    return json.loads(content)


def _normalize_and_validate(config):
    """Convert regexps to regexp type, validate all fields."""
    for color in config.get('colors', {}).values():
        if (not color.startswith('#')
                or len(color) != 7
                or color.strip('01234567890abcdefABCDEF') != '#'):
            raise ValueError("Invalid color '%s' in config" % color)
    try:
        config['colors'] = {
            re.compile('^%s$' % k): v
            for (k, v) in config.get('colors', {}).items()
        }
    except re.error:
        for regexp in config.get('colors', {}):
            try:
                re.compile('^%s$' % regexp)
            except re.error as e:
                raise ValueError("Invalid regexp '%s' in config: %s"
                                 % (regexp, e))

    # TOOD(csilvers): finish validation code

    return config


def load(args):
    """Return a configuration object based on commandline arguments.

    `args` should be a value returned by an argparse parser.parse_args()
    call.  If it includes `config` then the config is read from that
    json file.  It can also include other arguments to override/augment
    the values from the config file.  (This is necessary if `config` is
    not specified.)
    """
    if hasattr(args, 'config'):
        config = _read(args.config)
        config['configDir'] = os.path.dirname(os.path.abspath(args.config))
    else:
        config = {'configDir': os.path.dirname(os.path.abspath(__file__))}

    if hasattr(args, 'jenkins_base'):
        config['jenkinsBase'] = args.jenkins_base
    if hasattr(args, 'jenkins_username'):
        config['jenkinsAuth'] = {
            "username": args.jenkins_username,
            "password": args.jenkins_password,
        }
    if hasattr(args, 'keeper_record_id'):
        config['jenkinsAuth'] = {"keeperRecordId": args.keeper_record_id}

    if hasattr(args, 'grouping_parameter'):
        config['groupingParameter'] = args.grouping_parameter
    if hasattr(args, 'title_parameter'):
        config['titleParameter'] = args.title_parameter

    if hasattr(args, 'colors'):
        # This is specified as a list of strings that look like
        # "#RRGGBB:regexp".
        config['colors'] = {}
        for color_string in reversed(args.colors):
            (color, regexp) = color_string.split(':')
            config['colors'][regexp] = color

    if hasattr(args, 'datadir'):
        config['datadir'] = args.datadir
    if hasattr(args, 'download_threads'):
        config['downloadThreads'] = args.download_threads

    config = _normalize_and_validate(config)

    return config
