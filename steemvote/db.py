import logging
import os
import threading

import peewee

from steemvote.models import Comment

database = peewee.SqliteDatabase(None)

class DBVersionError(Exception):
    """Exception raised when an incompatible database version is encountered."""
    pass

class TrackedComment(object):
    """A comment with additional metadata."""
    def __init__(self, comment, reason_type, reason_value):
        self.comment = comment
        self.reason_type = reason_type
        self.reason_value = reason_value

class BaseDBModel(peewee.Model):
    class Meta:
        database = database

class DBConfig(BaseDBModel):
    key = peewee.CharField()
    value = peewee.CharField()

class DBComment(BaseDBModel):
    # Comment identifier.
    identifier = peewee.CharField(unique=True)
    # Type of reason why this comment is voted on.
    reason_type = peewee.CharField()
    # Value for why this comment is voted on.
    reason_value = peewee.CharField()
    # Whether this comment is being tracked.
    tracked = peewee.BooleanField()
    # Whether this comment has been voted on.
    voted = peewee.BooleanField()

class DB(object):
    """Database for storing data."""
    # Current database version.
    db_version = '0.1.0'

    def __init__(self, config):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)

        self.path = config.get('database_path', 'database.db')
        self.db = database
        self.db.init(self.path)
        self.db.connect()

        DBConfig.create_table(fail_silently=True)
        DBComment.create_table(fail_silently=True)
        self.check_version()

        self.lock = threading.RLock()
        # {identifier: TrackedComment, ...}
        self.tracked_comments = {}

    def check_version(self):
        """Check the database version and update it if possible."""
        version = self.get_version()
        if version < '0.1.0':
            raise DBVersionError('Invalid database version (%s)' % version)
        # Handle future db versions.
        elif version > self.db_version:
            raise DBVersionError('Stored database version (%s) is greater than current version (%s)' % (version, self.db_version))

    def get_version(self):
        """Get the stored database version."""
        query = DBConfig.select().where(DBConfig.key == 'db_version')
        if query.exists():
            version = query.get().value
        else:
            self.set_version()
            version = self.db_version
        return version

    def set_version(self):
        """Store the current database version."""
        DBConfig.create(key='db_version', value=self.db_version)

    def load(self, steem):
        """Load state."""
        # Load the comments to be voted on.
        for c in DBComment.select().where((DBComment.tracked == True) & (DBComment.voted == False)):
            comment = Comment(steem, c.identifier)
            self.tracked_comments[comment.identifier] = TrackedComment(comment, c.reason_type, c.reason_value)

    def close(self):
        self.db.close()

    def add_comment(self, comment, reason_type, reason_value):
        """Add a comment to be voted on later."""
        with self.lock:
            # Check if the post is already in the database.
            if DBComment.select().where(DBComment.identifier == comment.identifier).exists():
                return False

            # Add the comment.
            DBComment.create(identifier=comment.identifier, reason_type=reason_type, reason_value=reason_value,
                    tracked=True, voted=False)
            self.tracked_comments[comment.identifier] = TrackedComment(comment, reason_type, reason_value)
            return True

    def add_comment_with_author(self, comment):
        """Add a comment to be voted on later due to its author."""
        added = self.add_comment(comment, 'author', comment.author)
        if added:
            self.logger.info('Added %s by %s' % (comment.identifier, comment.author))

    def add_comment_with_delegate(self, comment, delegate_name):
        """Add a comment to be voted on later due to a delegate voter."""
        added = self.add_comment(comment, 'delegate', delegate_name)
        if added:
            self.logger.info('Added %s voted for by %s' % (comment.identifier, delegate_name))

    def update_voted_comments(self, comments):
        """Update comments that have been voted on."""
        with self.lock:
            for comment in comments:
                c = DBComment.select().where(DBComment.identifier == comment.identifier).get()
                c.tracked = False
                c.voted = True
                c.save()

            self.remove_tracked_comments([i.identifier for i in comments])

    def get_tracked_comments(self, with_metadata=True):
        """Get the comments that are being tracked.

        If with_metadata is False, only Comment instances
        will be returned.
        """
        with self.lock:
            comments = list(self.tracked_comments.values())
            if not with_metadata:
                comments = [i.comment for i in comments]
        return comments

    def remove_tracked_comments(self, identifiers):
        """Stop tracking comments with the given identifiers."""
        with self.lock:
            for identifier in identifiers:
                c = DBComment.select().where((DBComment.identifier == identifier) & (DBComment.tracked == True) & (DBComment.voted == False))
                if c.exists():
                    c.get().delete_instance()

                if identifier in self.tracked_comments:
                    del self.tracked_comments[identifier]
