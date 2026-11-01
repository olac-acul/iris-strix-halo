#!/bin/sh
# Local changes to Iris, applied before compiling.
#
# Every patch is optional: if Iris's code changes and a patch no longer
# applies, the build carries on and Iris behaves like upstream.
#
# Usage: patch-iris.sh [source dir]   (default /opt/iris)
set -e
cd "${1:-/opt/iris}"

# --------------------------------------------------------------------------
# cli-size: the interactive session starts at the size given in
# IRIS_CLI_WIDTH / IRIS_CLI_HEIGHT. Upstream always starts at 256x256 and
# ignores -W/-H. Values outside 64..4096 are ignored; rounded down to 16.
# --------------------------------------------------------------------------
if grep -q 'state.width = CLI_DEFAULT_WIDTH;' iris_cli.c \
   && grep -q 'state.height = CLI_DEFAULT_HEIGHT;' iris_cli.c \
   && grep -q '^int iris_cli_run(' iris_cli.c; then
    awk '
        /^int iris_cli_run\(/ {
            print "/* iris-cockpit: starting size of the interactive session */"
            print "static int cli_env_size(const char *name, int def) {"
            print "    const char *v = getenv(name);"
            print "    int n = v ? atoi(v) : 0;"
            print "    return (n >= 64 && n <= 4096) ? (n / 16) * 16 : def;"
            print "}"
            print ""
        }
        { print }
    ' iris_cli.c > iris_cli.c.new && mv iris_cli.c.new iris_cli.c
    sed -i \
        -e 's|state.width = CLI_DEFAULT_WIDTH;|state.width = cli_env_size("IRIS_CLI_WIDTH", CLI_DEFAULT_WIDTH);|' \
        -e 's|state.height = CLI_DEFAULT_HEIGHT;|state.height = cli_env_size("IRIS_CLI_HEIGHT", CLI_DEFAULT_HEIGHT);|' \
        iris_cli.c
    echo "patch cli-size: applied"
else
    echo "WARNING patch cli-size: Iris code changed, not applied (interactive session starts at 256x256)"
fi
