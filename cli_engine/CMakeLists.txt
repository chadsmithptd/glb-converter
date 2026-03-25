cmake_minimum_required(VERSION 3.14)
project(StepMetricsCli)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(StepMetricsCli StepMetricsCli.cpp)

if(DEFINED OPEN_CASCADE_DIR)
    # User-supplied OCCT installation (e.g. Windows 3rdPartyLibs)
    set(OpenCASCADE_DIR "${OPEN_CASCADE_DIR}/cmake")
    find_package(OpenCASCADE REQUIRED)
    target_include_directories(StepMetricsCli PRIVATE ${OpenCASCADE_INCLUDE_DIR})
    target_link_libraries(StepMetricsCli PRIVATE ${OpenCASCADE_LIBRARIES})
    target_link_directories(StepMetricsCli PRIVATE "${OPEN_CASCADE_DIR}/win64/vc14/lib")
else()
    # System-installed OCCT (Ubuntu apt packages on Linux / Railway).
    # apt installs headers to /usr/include/opencascade and .so files to
    # /usr/lib/x86_64-linux-gnu — no pkg-config .pc files are shipped.

    # Try cmake config first (works if OpenCASCADEConfig.cmake is on path)
    list(APPEND CMAKE_PREFIX_PATH "/usr/lib/x86_64-linux-gnu/cmake")
    find_package(OpenCASCADE QUIET)

    if(OpenCASCADE_FOUND)
        message(STATUS "Found OpenCASCADE via cmake config")
        target_include_directories(StepMetricsCli PRIVATE ${OpenCASCADE_INCLUDE_DIR})
        target_link_libraries(StepMetricsCli PRIVATE ${OpenCASCADE_LIBRARIES})
    else()
        # Direct fallback: find headers, link against library names directly.
        # The linker finds libTK*.so in /usr/lib/x86_64-linux-gnu automatically.
        find_path(OCCT_INCLUDE_DIR Standard.hxx
            PATHS /usr/include/opencascade /usr/local/include/opencascade
            REQUIRED)
        message(STATUS "Found OCCT headers at: ${OCCT_INCLUDE_DIR}")

        target_include_directories(StepMetricsCli PRIVATE ${OCCT_INCLUDE_DIR})
        target_link_libraries(StepMetricsCli PRIVATE
            TKernel TKMath TKBRep TKG2d TKG3d TKGeomBase TKGeomAlgo TKTopAlgo
            TKMesh TKBO TKShHealing
            TKXSBase TKSTEPBase TKSTEPAttr TKSTEP209 TKSTEP
            TKXCAF TKXDESTEP TKRWMesh TKLCAF TKCDF)
    endif()
endif()
