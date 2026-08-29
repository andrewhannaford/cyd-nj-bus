Import("env")
import os
import subprocess

# Windows workaround (see cyd-ms01-dashboard for the original writeup): PlatformIO/
# SCons's default cmd.exe-based spawn path mangles argv on long ESP32 command lines,
# dropping the trailing source-file argument. This re-tokenizes the real command line
# by hand (toggling quote state anywhere in the string, not just at word starts) and
# dispatches via subprocess.run(shell=False) instead.
PROJECT_DIR = env.subst("$PROJECT_DIR")


def _tokenize(cmdline):
    tokens = []
    current = []
    in_quotes = False
    for c in cmdline:
        if c == '"':
            in_quotes = not in_quotes
        elif c.isspace() and not in_quotes:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(c)
    if current:
        tokens.append("".join(current))
    return tokens


def _spawn(sh, escape, cmd, args, spawnenv):
    full_env = dict(os.environ)
    full_env.update(spawnenv)
    cmdline = " ".join(str(a) for a in args)
    real_args = _tokenize(cmdline)
    try:
        return subprocess.run(real_args, env=full_env, shell=False, cwd=PROJECT_DIR).returncode
    except OSError:
        return subprocess.run(cmdline, env=full_env, shell=True, cwd=PROJECT_DIR).returncode


env["SPAWN"] = _spawn
