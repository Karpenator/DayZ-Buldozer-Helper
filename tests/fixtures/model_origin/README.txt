These are generated test boxes, with no third-party game assets.

The source MLOD files were compiled on 2026-10-12 with the installed
DayZ Tools Binarize. The compiled fixtures are ODOL version 55.

Each visual box has bounds (0,0,0) through (4,6,2).
Geometry is identical unless its name says geometry_taller (top Y=8).
memory_taller adds a Memory LOD vertex at Y=20.
The basic, geometry_taller and memory_taller files set autocenter=0
in Geometry. The auto_* files set it to 1. default_auto omits it.
visual_only_* have no Geometry LOD and set the property only on the
visual LOD (or omit it for visual_only_no_auto).

The test reads BoundingCenter from the compiled fixtures independently
of terrain_editor.p3d and compares it to the MLOD loader's source_center.
All ten fixtures were generated in one Binarize run with these options:
-targetBonesInterval=56 -maxProcesses=1 -always -silent
-addon=<source folder> -textures=<output textures> -binPath=P:\
<source folder> <output folder>
The working directory was P:\. No game or server was started.
