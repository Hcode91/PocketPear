"""SpringBoard's side of the app's life: the workspace port UIKit checks for at launch."""
from services.base import MigServer


class Workspace(MigServer):
    name = 'workspace'


class SystemApp(MigServer):
    """SpringBoard itself (PurpleSystemAppPort): apps tell it about their state."""
    name = 'springboard'
