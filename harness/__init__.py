"""The Kind harness: cluster, images, deploy, and the pytest selections.

The harness is a Python package and a `k8s-tests` console script. It replaces the
Makefile and `hack/*.sh` scripts this repository used to have; every variable those
read is still read from the environment, and the resolved values are exported to the
checks, so a run behaves the way it did and can be installed with `pip` and run from
any directory.
"""
