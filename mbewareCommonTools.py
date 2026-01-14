import os
import colorist
from typing import Any




############################################################################################
# Logger utils
############################################################################################
from logging import DEBUG, INFO, WARN, ERROR, CRITICAL , FATAL
import logging

_mbeware_loggers={}


def createlogger(logname):
    global _mbeware_loggers
    logger = logging.getLogger(logname)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(f'%(asctime)s - {colorist.Color.RED}%(levelname)s{colorist.Color.OFF} - %(message)s')

    # file
    if os.path.dirname(logname) == "":
        logfullpath = f'/tmp/log/{logname}'
    else: 
        logfullpath = logname
    os.makedirs(os.path.dirname(logfullpath), exist_ok=True)

    file_handler = logging.FileHandler(logfullpath)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    #strerr
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # add to the logger collection
    _mbeware_loggers[logname]=logger
    logger.debug(f"createlogger: logger {logname} created")
    return logger

def listLoggers():
    pass

def change_loglevel(loggername,newlevel):
    global _mbeware_loggers
    loclogger = _mbeware_loggers.get(loggername)
    if loclogger is not None:
        for handler in loclogger.handlers:
            handler.setLevel(newlevel)


############################################################################################
# Exit code
############################################################################################


class ExitCode():
# Standard exit codes 
    Success = 0                 # The command or script executed without errors.
    General_error = 1           # A generic error occurred during execution.
    Misuse_of_builtins = 2      # Incorrect usage of a shell built-in command
    Timeout = 124               # Timeout
    Not_executable = 126        # Permission denied or command not executable.
    Not_found = 127             # The command is not recognized or available in the environment’s PATH.
    Invalid_exit_argument = 128 # An invalid argument was provided to the exit command.
    SIGINT = 130                # Script terminated by Ctrl+C (SIGINT)
    SIGKILL = 137               # Script terminated by SIGKILL (e.g., kill -9 or out-of-memory killer).
    Segfault = 139              # Indicates a segmentation fault occurred in the program.
    SIGTERM = 143               # Script terminated by SIGTERM (e.g., kill command without -9).
    Out_of_range = 255          # Typically, this happens when a script or command exits with a number > 255.
# Generaly accepted codes
    Invalid_argument=3          # An incorrect or missing argument was provided to the script.
    Input_output_error=5        # Failure to read or write data.
    No_such_target=6            # The specified device or address is unavailable.
    Commandline_error=64        # General syntax error in the command-line arguments.
    Data_format_error=65        # The input data format is incorrect or unexpected.
    Cannot_open_input=66        # A specified file or input cannot be accessed.
    Address_unknown=67          # Invalid or unknown destination address.
    Hostname_unknown=68         # Unable to resolve the specified host.
    Service_unavailable=69      # A service required to complete the task is unavailable.
    Internal_error=70           # An unhandled error occurred in the software or script logic.
    Operating_systen_error=71   # Generic system-related error, such as insufficient resources.
    Critical_OS_file_missing=72 # Required system files are not accessible or missing.
    Cannot_create=73            # Failure to create a file or directory.
    IO_error=74                 # Generic input/output failure.
    Temporary_failure=75        # A temporary condition caused the failure (e.g., network issue).
    Remote_protocol_error=76    # An error occurred in a remote communication protocol.
    Permission_denied=77        # The user or process lacks sufficient privileges.
    Configuration_error=78      # An issue occurred in configuration files or settings.

ExitCodeDesc={
    ExitCode.Success : "The command or script executed without errors.",
    ExitCode.General_error:" A generic error occurred during execution.",
    ExitCode.Misuse_of_builtins:" Incorrect usage of a shell built-in command",
    ExitCode.Timeout:"Timeout",
    ExitCode.Not_executable:" Permission denied or command not executable.",
    ExitCode.Not_found:" The command is not recognized or available in the environment's PATH.",
    ExitCode.Invalid_exit_argument:" An invalid argument was provided to the exit command.",
    ExitCode.SIGINT:" Script terminated by Ctrl+C (SIGINT)",
    ExitCode.SIGKILL:" Script terminated by SIGKILL (e.g., kill -9 or out-of-memory killer).",
    ExitCode.Segfault:" Indicates a segmentation fault occurred in the program.",
    ExitCode.SIGTERM:" Script terminated by SIGTERM (e.g., kill command without -9).",
    ExitCode.Out_of_range:" Typically, this happens when a script or command exits with a number > 255.",
# Generaly accepted codes
    ExitCode.Invalid_argument:" An incorrect or missing argument was provided to the script.",
    ExitCode.Input_output_error:" Failure to read or write data.",
    ExitCode.No_such_target:" The specified device or address is unavailable.",
    ExitCode.Commandline_error:" General syntax error in the command-line arguments.",
    ExitCode.Data_format_error:" The input data format is incorrect or unexpected.",
    ExitCode.Cannot_open_input:" A specified file or input cannot be accessed.",
    ExitCode.Address_unknown:" Invalid or unknown destination address.",
    ExitCode.Hostname_unknown:" Unable to resolve the specified host.",
    ExitCode.Service_unavailable:" A service required to complete the task is unavailable.",
    ExitCode.Internal_error:" An unhandled error occurred in the software or script logic.",
    ExitCode.Operating_systen_error:" Generic system-related error, such as insufficient resources.",
    ExitCode.Critical_OS_file_missing:"Required system files are not accessible or missing.",
    ExitCode.Cannot_create:"Failure to create a file or directory.",
    ExitCode.IO_error:"Generic input/output failure.",
    ExitCode.Temporary_failure:"A temporary condition caused the failure (e.g., network issue).",
    ExitCode.Remote_protocol_error:"An error occurred in a remote communication protocol.",
    ExitCode.Permission_denied:"The user or process lacks sufficient privileges.",
    ExitCode.Configuration_error:"An issue occurred in configuration files or settings."
}



############################################################################################
# Config utils
############################################################################################
import tomli 

def load_config(config_path:str)->dict[str,Any]:
    try:
        current_directory = os.getcwd()
        print("The current working directory is:", current_directory)
        print(config_path)
        with open(config_path, "rb") as f:
            return tomli.load(f)
    except Exception as e:
        temp_logger = _mbeware_loggers.get(__name__)
        print(f"Error loading config file '{config_path}': {e}")
        exit(ExitCode.Configuration_error)