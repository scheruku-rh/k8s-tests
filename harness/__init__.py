"""The Kind harness: cluster, images, deploy, and the pytest selections.

The harness used to be a Makefile and a set of `hack/*.sh` scripts; it is a Python
package now, so it can be installed (`pip install k8s-tests`) and run from anywhere.
The knobs are unchanged: every variable the Makefile read is still read from the
environment, and the resolved values are still exported to the checks.
"""
