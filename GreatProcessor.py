"""GreatProcess.py - Hold values for the current parsing cycle."""
import multiprocessing
import pickle


class GreatProcessor:
    """gp - Hold values for the current parsing cycle."""

    def __init__(self) -> None:
        """Set version info and ChangeSet_Dict."""
        self.PURGE_LIST = []
        self.Table_Array = []
        self.Version_Name = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        self.Old_Version_Name = 0
        self.VID = 0
        self.Old_VID = 0
        self.Change_List = None
        self.ChangeSet_Dict = {}
        self.Manager = None
        self.Shared_ChangeSet_Dict_List = None

    def push_set_to_main(self) -> None:
        """Push pickled ChangeSet_Dict to Shared_ChangeSet_Dict_List."""
        self.Shared_ChangeSet_Dict_List.append(pickle.dumps(self.ChangeSet_Dict))
        return

    def start_manager(self) -> None:
        """Start multiprocessing manager list."""
        self.Manager = multiprocessing.Manager()
        self.Shared_ChangeSet_Dict_List = self.Manager.list()
        return

    def stop_manager(self) -> None:
        """Update ChangeSet_Dict with shared(s) and stop multiprocessing manager."""
        for shared in self.Shared_ChangeSet_Dict_List:
            self.ChangeSet_Dict.update(pickle.loads(shared))  # noqa: S301

        del self.Manager
        del self.Shared_ChangeSet_Dict_List
        self.Manager = None
        self.Shared_ChangeSet_Dict_List = []
        return

    def reset_cs(self) -> None:
        """Reset Change_List and ChangeSet_Dict for the next version."""
        del self.Change_List
        del self.ChangeSet_Dict
        self.Change_List = None
        self.ChangeSet_Dict = {}
        return
