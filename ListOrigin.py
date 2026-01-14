#!/usr/bin/env python3
from mbe_ofslib import OFSManager, OFSinfo
import argparse


def display_overlay_info(overlay_info:dict[str,OFSinfo],filter="all"):
    OFS_entry:OFSinfo

    for OFS_entry in overlay_info.values():
        if OFS_entry.base_dir == filter or filter == "all":
            print()
            print(f"{OFS_entry.base_dir}  \t\tworkdir: {OFS_entry.work_dir}")
            print(f"  ├── upperdir: {OFS_entry.upper_dir}")
            for j, lowerdir in enumerate(OFS_entry.lower_dir):
                if j == len(OFS_entry.lower_dir) - 1:
                    print(f"  └── lowerdir {-(j+1)}: {lowerdir}")
                else:
                    print(f"  ├── lowerdir {-(j+1)}: {lowerdir}")
def HandleList(args):
    myOFS = OFSManager()
    for base_folder in myOFS.info.keys():
        print(base_folder)

def HandleInfo(args):
    myOFS = OFSManager()
    filter = "".join(args.overlay[0]).rstrip("/")
    
    if filter == "all" or filter in myOFS.info.keys():
        display_overlay_info(myOFS.info,filter)
    else:
        print(f"Overlay <{filter}> not found")  

def HandleShow(args):
    print(f"List overlay directories and files in a tree {args.overlay =}")

def HandleClean(args):
    print(f"Remove version of files in lower directories {args.overlay =}")

def HandleNewLayer(args):
    print(f"Create a new uppermost directory {args.overlay =}")


def HandleTest(args):
    myOFS = OFSManager()
    print(f"{myOFS.info =}")


# the leftmost layer of the overlay mount command is the layer nearest to the upperdir. user -> ulfs -> llfs_2 -> llfs_1


def setup_argparsers():
    parser = argparse.ArgumentParser()
    
    subparsers = parser.add_subparsers()
    parser_test = subparsers.add_parser('test')
    parser_list = subparsers.add_parser('list')
    parser_info = subparsers.add_parser('info')
    parser_show = subparsers.add_parser('show')
    parser_clean = subparsers.add_parser('clean')
    parser_newlayer = subparsers.add_parser('newlayer')

    parser_test.set_defaults(func=HandleTest)
    parser_list.set_defaults(func=HandleList)
    parser_info.set_defaults(func=HandleInfo)
    parser_clean.set_defaults(func=HandleClean)
    parser_newlayer.set_defaults(func=HandleNewLayer)
    parser_show.set_defaults(func=HandleShow)

    parser_test.add_argument('test',action='store_true',help="test",default=False)

    parser_list.add_argument('list',action='store_true',help="List all overlay filesytems",default=False)

    parser_info.add_argument('overlay',nargs=1,type=str,help="List overlay structure", default=None)

    parser_show.add_argument('overlay',nargs=1,type=str,help="List overlay directories and files in a tree", default=None)
    parser_show.add_argument('--DirOnly',action='store_true',help="Only list directories")
    parser_show.add_argument('--MaxDepth',type=int,help="Max number of layers to list")

    subparsers_clean = parser_clean.add_subparsers()
    parser_cleanAll = subparsers_clean.add_parser('All')
    parser_cleanDir = subparsers_clean.add_parser('Dir')
    parser_cleanFile = subparsers_clean.add_parser('File')

    parser_cleanAll.add_argument('overlay',help="Remove version of files in lower directories", default=None)

    parser_cleanDir.add_argument('overlay',help="Remove version of files in lower directories", default=None)
    parser_cleanDir.add_argument('--Dir',dest='DirName',help="Remove all version for all files in <DirName> in lower directories")

    parser_cleanFile.add_argument('overlay',help="Remove version of files in lower directories", default=None)
    parser_cleanFile.add_argument('--File',dest='DirName',help="Remove all version for <FileName> in lower directories")

    parser_newlayer.add_argument('overlay',help="Create a new uppermost directory", default=None)
    parser_newlayer.add_argument('newDirectory',help="Create a new uppermost directory")
    parser_newlayer.add_argument('--Run',action='store_true',help="don't ask for confirmation and run the script")
    parser_newlayer.add_argument('--Workdir',nargs=1,help="specify the working directory")

    return parser
    


if __name__ == "__main__":
    import sys
    if  len(sys.argv) == 1:
        HandleTest(sys.argv)
        exit(0)


    parser = setup_argparsers()
    args = parser.parse_args()
    args.func(args)


# -List : List all overlay filesytems
# -Info <OverlayFS>: List overlay structure
# -Show <OverlayFS>: List overlay directories and files in a tree
#    root = uppermost directory
#    options : --DirOnly : Only list directories
#    options : --MaxDeep : Max number of layers to list
# -Clean : 
#   --All <OverlayFS>  : Remove all version for all files in lower directories
#   --Dir <OverlayFS> <DirName>: Remove all version for all files in <DirName> in lower directories
#   --File <OverlayFS> <FileName>: Remove all version for <FileName> in lower directories
#   If there is a "rubout" file, delete it and all versions in lower directories

# -Newlayer <NewUpperDir> 
#   --Run : don't ask for confirmation and run the script
#   --Workdir <WorkDir> : specify the working directory
#    generate a command line to Create a uppermost directory and working directory with the name "."<NewUpperDir>"_work" or "<WorkDir>"
#    generate a command line to unmount the old overlayfs
#    generate a command line to delete the old working directory
#    generate a command line to rename the old upper directory to OverlayFS name with "Level_<N>" where N is the new layer number
#    generate the new mount command line with the old (renamed)upper directory as the topmost new lower directory
#    generate script to run the above commands
#    display the script and ask for confirmation to run it
#    execute the script




