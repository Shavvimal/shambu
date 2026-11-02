"""`shambu cad`: offline gates on exported CAD files.

What it handles: STL exports and 3MF objects of parts designed in a CAD program, the provenance records
`shambu cad stamp` writes beside them, and a declarative gate spec (`shambu.cad/1`). It checks one closed solid,
volume and size against the numbers the CAD session reported, hole and boss inventories, hole and insert-pocket
fits, clearance between placed parts, insertion and rotation sweeps, and the change between two revisions.

What it does not do: it never starts, drives or imports a CAD program, an MCP server or Node; the skill
`designing-for-print` drives the CAD session and calls these verbs as an independent judge. It repairs no mesh,
slices nothing and prints nothing (cli.py prints). Booleans and distance engines live in `cad/geom`, behind the
optional `geom` extra (numpy, scipy, manifold3d); every other module here is pure Python.
"""
