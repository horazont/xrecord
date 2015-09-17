#!/usr/bin/python3
import configparser
import functools
import os
import re
import shutil
import signal
import subprocess
import tempfile

import xdg.BaseDirectory

from datetime import datetime, timedelta


re_upper_left_x = re.compile(r"absolute upper-left x:\s*([0-9]+)", re.I)
re_upper_left_y = re.compile(r"absolute upper-left y:\s*([0-9]+)", re.I)
re_width = re.compile(r"width:\s*([0-9]+)", re.I)
re_height = re.compile(r"height:\s*([0-9]+)", re.I)


re_ffmpeg_progress = re.compile(r"time=([0-9]{2}):([0-9]{2}):([0-9]{2}).([0-9]{2})")


INITIAL_FILE_NAME = "first.mkv"


def discover_geometry(args):
    call = ["xwininfo", "-stats"]

    if args.window_id is not None:
        call.append("-id")
        call.append(args.window_id)
    elif args.window_name is not None:
        call.append("-name")
        call.append(args.window_name)
    elif args.root:
        call.append("-root")

    output = subprocess.check_output(call).decode()
    x = int(re_upper_left_x.search(output).group(1))
    y = int(re_upper_left_y.search(output).group(1))
    w = int(re_width.search(output).group(1))
    h = int(re_height.search(output).group(1))

    return x, y, w, h


def load_config():
    config = configparser.ConfigParser(delimiters="=")
    for path in xdg.BaseDirectory.load_config_paths("xrecord"):
        config.read(os.path.join(path, "config.ini"))
    return config


def get_cachedir(config):
    cachedir = config.get("general",
                          "cachedir",
                          fallback=xdg.BaseDirectory.save_cache_path("xrecord"))

    token = "{date}-{pid}".format(
        date=datetime.utcnow().isoformat(),
        pid=os.getpid()
    )

    cachedir = os.path.join(cachedir, token)

    os.makedirs(cachedir)

    return cachedir


def extract_ffmpeg_time(line):
    m = re_ffmpeg_progress.search(line)
    if m is not None:
        groups = m.groups()
        return timedelta(hours=int(groups[0]),
                         minutes=int(groups[1]),
                         seconds=int(groups[2]),
                         microseconds=int(groups[3])*10000)
    return None


def ffmpeg_progress(progress_cb, proc):
    progress_cb(timedelta())
    while proc.returncode is None:
        time = extract_ffmpeg_time(proc.stderr.readline().decode())
        if time is not None:
            progress_cb(time)
        proc.poll()
    progress_cb(None)


def ffmpeg_capture_duration(proc):
    duration = None
    while proc.returncode is None:
        line = proc.stderr.readline().decode()
        time = extract_ffmpeg_time(line)
        if time is not None:
            duration = time
        proc.poll()

    return duration

def run_with_signal_forwarding(call, *, wait_fun=None, **kwargs):
    interrupted = False
    def term_handler(sig, _):
        nonlocal proc, interrupted
        interrupted = True
        proc.terminate()

    proc = subprocess.Popen(call, **kwargs)
    signal.signal(signal.SIGINT, term_handler)
    signal.signal(signal.SIGTERM, term_handler)
    try:
        if not wait_fun:
            proc.wait()
            result = None
        else:
            result = wait_fun(proc)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    return proc, result


def record(cachefile, framerate, display, geometry):
    ffmpeg_call = [
        "ffmpeg",
        "-nostdin"
    ]

    ffmpeg_call.extend([
        "-video_size", "{2}x{3}".format(*geometry),
        "-framerate", str(framerate),
        "-f", "x11grab",
        "-i", "{display}+{0},{1}".format(*geometry, display=display),
        "-c:v", "libx264",
        "-qp", "0",
        "-preset", "ultrafast",
        cachefile
    ])

    proc, duration = run_with_signal_forwarding(ffmpeg_call,
                                                stderr=subprocess.PIPE,
                                                wait_fun=ffmpeg_capture_duration)
    if proc.returncode not in [0, 255]:
        raise subprocess.CalledProcessError(proc.returncode, ffmpeg_call)

    return duration


def open_output_file(pattern):
    if not "{" in pattern:
        return open(pattern, "xb")

    for i in range(1000):
        try:
            return open(pattern.format(i), "xb")
        except FileExistsError:
            continue
    raise FileExistsError(pattern)


def encode(source_file, output_file, config_section, progress_cb):
    ffmpeg_call = [
        "ffmpeg",
        "-nostdin",
        "-i", source_file,
    ]

    for key in config_section:
        if not key.startswith("-"):
            continue
        ffmpeg_call.append(key)
        value = config_section[key]
        if value:
            ffmpeg_call.append(value)

    ffmpeg_call.append("-")

    kwargs = {
        "stdout": output_file,
        "stderr": subprocess.DEVNULL
    }
    if progress_cb is not None:
        kwargs["stderr"] = subprocess.PIPE
        kwargs["wait_fun"] = functools.partial(
            ffmpeg_progress, progress_cb)

    proc, _ = run_with_signal_forwarding(ffmpeg_call, **kwargs)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, ffmpeg_call)


def print_progress(duration, time):
    if time is None:
        time = duration
    print(
        "\r\x1b[Kencoding: {:5.1f}%".format(
            time.total_seconds() / duration.total_seconds() * 100
        ),
        end="")
    if time == duration:
        print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "-w", "--window",
        action="store_true",
        help="Select the window to record by clicking on it"
    )
    source_group.add_argument(
        "-i", "--window-id",
        metavar="ID",
        help="Select the window by id"
    )
    source_group.add_argument(
        "-n", "--window-name",
        metavar="NAME",
        help="Select the window by name"
    )
    source_group.add_argument(
        "-f", "--root",
        action="store_true",
        help="Use the root window (whole screen)"
    )

    parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_false",
        help="Disable progress output"
    )

    parser.add_argument(
        "-r", "--framerate",
        metavar="RATE",
        type=int,
        help="Frames per second to grab",
        default=25
    )

    args = parser.parse_args()

    geometry = discover_geometry(args)
    config = load_config()
    display = os.environ.get("DISPLAY", ":0")
    cachedir = get_cachedir(config)

    if args.progress:
        progress_cb = print_progress
    else:
        progress_cb = None

    recording_filename = os.path.join(cachedir, INITIAL_FILE_NAME)
    with open_output_file(
            os.path.expanduser(config.get("encode", "output", fallback="~/out-{}.ogv")
            )) as dest:
        try:
            duration = record(recording_filename, args.framerate, display, geometry)
        except:
            os.unlink(dest.name)
            raise

        encode(
            recording_filename, dest, config["encode"],
            functools.partial(progress_cb, duration)
        )

    shutil.rmtree(cachedir)
