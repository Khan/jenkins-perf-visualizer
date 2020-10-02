"""Coalesce pipeline steps for a single build into a tree of 'nodes'.

This coalesces the steps from the Step data structure into a small
number of datapoints that's useful for visualizing.  Every time we
run a parallel() step, or start a new stage(), we create a new Node.
For simple Node's we just store the start-time and end-time.  But we
keep track of when the Node is sleeping or waiting for something
(e.g. due to a prompt() step), since that's useful info for visualization.

As with steps, nodes form a hierarchy: we start with a root node, and
every time we see a new stage(), say, it causes a new node to be
created which is a child of the root node.

The public API here is steps_to_nodes().
"""

# The modes that a job can be in.
RUNNING = "RUNNING"
SLEEPING = "Sleeping"  # running sleep()
WAITING = "Waiting"   # running waitFor() or prompt()
AWAITING_EXECUTOR = "Awaiting executor"   # waiting for a new  machine
NOT_RUNNING = "[not running]"


class Node(object):
    """Holds information about what one node is doing."""
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
            # in a not-running range between them.
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
