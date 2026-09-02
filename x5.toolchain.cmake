# X5 (aarch64) cross toolchain; used inside the pc_tros_solution_ubuntu22.04
# docker image with the board sysroot bind-mounted over /opt/tros/humble etc.
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(CMAKE_C_COMPILER /usr/bin/aarch64-linux-gnu-gcc)
set(CMAKE_CXX_COMPILER /usr/bin/aarch64-linux-gnu-g++)
# board shared libs' transitive deps are not all in the sysroot — don't try to
# resolve them at link time
set(CMAKE_EXE_LINKER_FLAGS_INIT "-Wl,--allow-shlib-undefined")
set(CMAKE_SHARED_LINKER_FLAGS_INIT "-Wl,--allow-shlib-undefined")
set(CMAKE_MODULE_LINKER_FLAGS_INIT "-Wl,--allow-shlib-undefined")
set(CMAKE_CXX_FLAGS_INIT "-Wno-psabi")
