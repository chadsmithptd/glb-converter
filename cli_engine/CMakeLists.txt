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
    # System-installed OCCT (Ubuntu apt packages on Linux / Railway)
    find_package(OpenCASCADE QUIET)
    if(OpenCASCADE_FOUND)
        target_include_directories(StepMetricsCli PRIVATE ${OpenCASCADE_INCLUDE_DIR})
        target_link_libraries(StepMetricsCli PRIVATE ${OpenCASCADE_LIBRARIES})
    else()
        # Fall back to pkg-config / manual discovery for apt-installed OCCT
        find_package(PkgConfig REQUIRED)
        pkg_check_modules(OCCT REQUIRED
            TKernel
            TKMath
            TKBRep
            TKGeomBase
            TKGeomAlgo
            TKTopAlgo
            TKMesh
            TKBO
            TKShHealing
            TKSTEP
            TKSTEP209
            TKSTEPAttr
            TKSTEPBase
            TKXCAF
            TKXDE
            TKXDESTEP
            TKRWMesh
            TKLCAF
            TKCDF
            TKVCAF
        )
        target_include_directories(StepMetricsCli PRIVATE ${OCCT_INCLUDE_DIRS})
        target_link_libraries(StepMetricsCli PRIVATE ${OCCT_LIBRARIES})
        target_compile_options(StepMetricsCli PRIVATE ${OCCT_CFLAGS_OTHER})
    endif()
endif()
