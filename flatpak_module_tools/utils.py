import pipes
import subprocess
import sys

def die(msg):
    print >>sys.stderr, msg
    sys.exit(1)

def check_call(args):
    print >>sys.stderr, "Running: " + ' '.join(pipes.quote(a) for a in args)
    rv = subprocess.call(args)
    if rv != 0:
        die("%s failed (exit status=%d)" % (args[0], rv))
