# -*- coding: utf-8 -*-
"""
priority/tree
~~~~~~~~~~~~~

Implementation of the Priority tree data structure.
"""
from __future__ import division

import sys

try:
    import Queue as queue
except ImportError:  # Python 3:
    import queue


PY3 = sys.version_info[0] == 3


class DeadlockError(Exception):
    """
    Raised when there are no streams that can make progress: all streams are
    blocked.
    """
    pass


class PriorityLoop(Exception):
    """
    An unexpected priority loop has been detected. The tree is invalid.
    """
    pass


class DuplicateStreamError(Exception):
    """
    An attempt was made to insert a stream that already exists.
    """
    pass


class MissingStreamError(KeyError, Exception):
    """
    An operation was attempted on a stream that is not present in the tree.
    """
    pass


class Stream(object):
    """
    Priority information for a given stream.

    :param stream_id: The stream ID for the new stream.
    :param weight: (optional) The stream weight. Defaults to 16.
    """
    def __init__(self, stream_id, weight=16):
        self.stream_id = stream_id
        self.weight = weight
        self.children = []
        self.parent = None
        self.child_queue = queue.PriorityQueue()
        self.active = True
        self.last_weight = 0
        self._deficit = 0

    def add_child(self, child):
        """
        Add a stream that depends on this one.

        :param child: A ``Stream`` object that depends on this one.
        """
        child.parent = self
        self.children.append(child)
        self.child_queue.put((self.last_weight, child))

    def add_child_exclusive(self, child):
        """
        Add a stream that exclusively depends on this one.

        :param child: A ``Stream`` object that exclusively depends on this one.
        """
        old_children = self.children
        self.children = []
        self.child_queue = queue.PriorityQueue()
        self.last_weight = 0
        self.add_child(child)

        for old_child in old_children:
            child.add_child(old_child)

    def remove_child(self, child, strip_children=True):
        """
        Removes a child stream from this stream. This is a potentially somewhat
        expensive operation.

        :param child: The child stream to remove.
        :param strip_children: Whether children of the removed stream should
            become children of this stream.
        """
        # To do this we do the following:
        #
        # - remove the child stream from the list of children
        # - build a new priority queue, filtering out the child when we find
        #   it in the old one
        self.children.remove(child)

        new_queue = queue.PriorityQueue()

        while not self.child_queue.empty():
            level, stream = self.child_queue.get()
            if stream == child:
                continue

            new_queue.put((level, stream))

        self.child_queue = new_queue

        if strip_children:
            for new_child in child.children:
                self.add_child(new_child)

    def schedule(self):
        """
        Returns the stream ID of the next child to schedule. Potentially
        recurses down the tree of priorities.
        """
        # Cannot be called on active streams.
        assert not self.active

        next_stream = None
        popped_streams = []

        # Spin looking for the next active stream. Everything we pop off has
        # to be rescheduled, even if it turns out none of them were active at
        # this time.
        try:
            while next_stream is None:
                # If the queue is empty, immediately fail.
                val = self.child_queue.get(block=False)
                popped_streams.append(val)
                level, child = val

                if child.active:
                    next_stream = child.stream_id
                else:
                    # Guard against the possibility that the child also has no
                    # suitable children.
                    try:
                        next_stream = child.schedule()
                    except queue.Empty:
                        continue
        finally:
            for level, child in popped_streams:
                self.last_weight = level
                level += (256 + child._deficit) // child.weight
                child._deficit = (256 + child._deficit) % child.weight
                self.child_queue.put((level, child))

        return next_stream

    # Custom repr
    def __repr__(self):
        return "Stream<id=%d, weight=%d>" % (self.stream_id, self.weight)

    # Custom comparison
    def __eq__(self, other):
        if not isinstance(other, Stream):  # pragma: no cover
            return False

        return self.stream_id == other.stream_id

    def __ne__(self, other):
        return not self.__eq__(other)

    def __lt__(self, other):
        if not isinstance(other, Stream):  # pragma: no cover
            return NotImplemented

        return self.stream_id < other.stream_id

    def __le__(self, other):
        if not isinstance(other, Stream):  # pragma: no cover
            return NotImplemented

        return self.stream_id <= other.stream_id

    def __gt__(self, other):
        if not isinstance(other, Stream):  # pragma: no cover
            return NotImplemented

        return self.stream_id > other.stream_id

    def __ge__(self, other):
        if not isinstance(other, Stream):  # pragma: no cover
            return NotImplemented

        return self.stream_id >= other.stream_id


class PriorityTree(object):
    """
    A HTTP/2 Priority Tree.

    This tree stores HTTP/2 streams according to their HTTP/2 priorities.
    """
    def __init__(self):
        # This flat array keeps hold of all the streams that are logically
        # dependent on stream 0.
        self._root_stream = Stream(stream_id=0, weight=1)
        self._root_stream.active = False
        self._streams = {0: self._root_stream}

    def _exclusive_insert(self, parent_stream, inserted_stream):
        """
        Insert ``inserted_stream`` beneath ``parent_stream``, obeying the
        semantics of exclusive insertion.
        """
        parent_stream.add_child_exclusive(inserted_stream)

    def insert_stream(self,
                      stream_id,
                      depends_on=None,
                      weight=16,
                      exclusive=False):
        """
        Insert a stream into the tree.

        :param stream_id: The stream ID of the stream being inserted.
        :param depends_on: (optional) The ID of the stream that the new stream
            depends on, if any.
        :param weight: (optional) The weight to give the new stream. Defaults
            to 16.
        :param exclusive: (optional) Whether this new stream should be an
            exclusive dependency of the parent.
        """
        if stream_id in self._streams:
            raise DuplicateStreamError("Stream %d already in tree" % stream_id)

        stream = Stream(stream_id, weight)

        if exclusive:
            assert depends_on is not None
            parent_stream = self._streams[depends_on]
            self._exclusive_insert(parent_stream, stream)
            self._streams[stream_id] = stream
            return

        if not depends_on:
            depends_on = 0

        parent = self._streams[depends_on]
        parent.add_child(stream)
        self._streams[stream_id] = stream

    def reprioritize(self,
                     stream_id,
                     depends_on=None,
                     weight=16,
                     exclusive=False):
        """
        Update the priority status of a stream already in the tree.

        :param stream_id: The stream ID of the stream being updated.
        :param depends_on: (optional) The ID of the stream that the stream now
            depends on. If ``None``, will be moved to depend on stream 0.
        :param weight: (optional) The new weight to give the stream. Defaults
            to 16.
        :param exclusive: (optional) Whether this stream should now be an
            exclusive dependency of the new parent.
        """
        def stream_cycle(new_parent, current):
            """
            Reports whether the new parent depends on the current stream.
            """
            parent = new_parent

            # Don't iterate forever, but instead assume that the tree doesn't
            # get more than 100 streams deep. This should catch accidental
            # tree loops. This is the definition of defensive programming.
            for _ in range(100):
                parent = parent.parent
                if parent.stream_id == current.stream_id:
                    return True
                elif parent.stream_id == 0:
                    return False

            raise PriorityLoop(
                "Stream %d is in a priority loop." % new_parent.stream_id
            )  # pragma: no cover

        try:
            current_stream = self._streams[stream_id]
        except KeyError:
            raise MissingStreamError("Stream %d not in tree" % stream_id)

        # Update things in a specific order to make sure the calculation
        # behaves properly. Specifically, we first update the weight. Then,
        # we check whether this stream is being made dependent on one of its
        # own dependents. Then, we remove this stream from its current parent
        # and move it to its new parent, taking its children with it.
        if depends_on:
            # TODO: What if we don't have the new parent?
            new_parent = self._streams[depends_on]
            cycle = stream_cycle(new_parent, current_stream)
        else:
            new_parent = self._streams[0]
            cycle = False

        current_stream.weight = weight

        # Our new parent is currently dependent on us. We should remove it from
        # its parent, and make it a child of our current parent, and then
        # continue.
        if cycle:
            new_parent.parent.remove_child(new_parent)
            current_stream.parent.add_child(new_parent)

        current_stream.parent.remove_child(
            current_stream, strip_children=False
        )

        if exclusive:
            new_parent.add_child_exclusive(current_stream)
        else:
            new_parent.add_child(current_stream)

    def remove_stream(self, stream_id):
        """
        Removes a stream from the priority tree.

        :param stream_id: The ID of the stream to remove.
        """
        try:
            child = self._streams.pop(stream_id)
        except KeyError:
            raise MissingStreamError("Stream %d not in tree" % stream_id)

        parent = child.parent
        parent.remove_child(child)

    def block(self, stream_id):
        """
        Marks a given stream as blocked, with no data to send.

        :param stream_id: The ID of the stream to block.
        """
        try:
            self._streams[stream_id].active = False
        except KeyError:
            raise MissingStreamError("Stream %d not in tree" % stream_id)

    def unblock(self, stream_id):
        """
        Marks a given stream as unblocked, with more data to send.

        :param stream_id: The ID of the stream to unblock.
        """
        # When a stream becomes unblocked,
        try:
            self._streams[stream_id].active = True
        except KeyError:
            raise MissingStreamError("Stream %d not in tree" % stream_id)

    # The iterator protocol
    def __iter__(self):  # pragma: no cover
        return self

    def __next__(self):  # pragma: no cover
        try:
            return self._root_stream.schedule()
        except queue.Empty:
            raise DeadlockError("No unblocked streams to schedule.")

    def next(self):  # pragma: no cover
        return self.__next__()
