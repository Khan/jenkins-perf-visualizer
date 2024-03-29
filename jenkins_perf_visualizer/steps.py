"""Process Jenkins steps for a single build into an execution tree.

A "step" is a single jenkins pipeline command: `parallel()`, `sh()`,
etc.  It's also a single row in the flow-pipeline page.

Some steps, like stage(), have a body that can have other steps inside
it.  This yields an execution tree of steps.  We take the html of
a "pipeline steps" jenkins page and return the root of the execution
tree.

The public API here is parse_pipeline_steps().
"""
import re

# These substrings/regexps are taken from the html of a pipeline-steps
# Jenkins page, e.g.
#   view-source:https://jenkins.khanacademy.org/job/deploy/job/webapp-test/lastSuccessfulBuild/flowGraphTable/
_ROW_RE = re.compile(
    r'<td style="padding-left: calc.var.--table-padding. \* (?P<indent>\d+).">'
    r'\s*<a tooltip="ID: (?P<id>\d+)" [^>]*>'
    r'\s*(?P<step_text>[^<]*)'
    r'\s*</a>'
    r'\s*</td>'
)
_WAIT_UNTIL_TEXT = "waitUntil - "   # pipeline text for waitUntil()
_PROMPT_TEXT = "input - "           # pipeline text for prompt()
_SLEEP_TEXT = "sleep - "            # pipeline text for sleep()
_NODE_TEXT = "node - "              # pipeline text for node()
_PARALLEL_TEXT = "parallel - "      # pipeline text for parallel()
_BRANCH_RE = re.compile(r'\(Branch: ([^)]*)\)')     # text within parallel()
_STAGE_RE = re.compile(r'stage block \(([^)]*)\)')  # text for stage()


def _html_unescape(s):
    """Unescape step_text, lamely.  TODO(csilvers): use a real parser."""
    return s.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')


class Step(object):
    """Important (to us) information about one executed pipeline "step".

    A "step" is a single jenkins pipeline command: `parallel()`, `sh()`,
    etc.  It's also a single row in the flow-pipeline page.
    """
    def __init__(self, id, indentation, step_text, previous_steps):
        """Populates all the fields we can from a given 'step' html.

        "step_text" is the stuff between the <a> and </a> in this:
           <a tooltip="ID: 4" href="/job/deploy/job/e2e-test/16585/execution/node/4/">Start of Pipeline - (16 min in block)</a>  # NoQA:L501
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
        self.is_waiting = (_WAIT_UNTIL_TEXT in step_text or
                           _PROMPT_TEXT in step_text)
        self.is_sleeping = _SLEEP_TEXT in step_text

        # True if we are allocating a new node (on a jenkins worker, say).
        self.is_new_worker = _NODE_TEXT in step_text
        # True if we start a new stage (via the stage() pipeline command).
        self.is_new_stage = bool(_STAGE_RE.search(step_text))
        # True if our children are executed in parallel.
        self.is_parallel_parent = _PARALLEL_TEXT in step_text
        # True if we are starting a new branch inside a parallel().
        self.is_branch_step = bool(_BRANCH_RE.search(step_text))

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
            m = _BRANCH_RE.search(step_text)
            return m.group(1)
        elif self.is_new_stage:
            m = _STAGE_RE.search(step_text)
            return m.group(1)
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
    steps = []
    for m in _ROW_RE.finditer(html):
        step_text = _html_unescape(m.group('step_text').strip())
        step = Step(m.group('id'), m.group('indent'), step_text, steps)
        steps.append(step)

    # Now patch over the elapsed-time bug for "Branch:" nodes.
    # (See the docstring for Step._elapsed_time(), above.)
    for step in steps:
        if step.is_branch_step:
            step.elapsed_time_ms = sum(c.elapsed_time_ms
                                       for c in step.children)

    return steps[0] if steps else None
