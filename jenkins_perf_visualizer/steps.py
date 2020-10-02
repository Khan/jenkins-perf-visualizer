"""Process Jenkins steps for a single build into an execution tree.

A "step" is a single jenkins pipeline command: `parallel()`, `sh()`,
etc.  It's also a single row in the flow-pipeline page.

Some steps, like stage(), have a body that can have other steps inside
it.  This yields an execution tree of steps.  We take the html of
a "pipeline steps" jenkins page and return the root of the execution
tree.

The public API here is parse_pipeline_steps.
"""
import re


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
        step_text = _html_unescape(step_text.strip())
        step = Step(id, indentation, step_text, steps)
        steps.append(step)

    # Now patch over the elapsed-time bug for "Branch:" nodes.
    # (See the docstring for Step._elapsed_time(), above.)
    for step in steps:
        if step.is_branch_step:
            step.elapsed_time_ms = sum(c.elapsed_time_ms
                                       for c in step.children)

    return steps[0] if steps else None
