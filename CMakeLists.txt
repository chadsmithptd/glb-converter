cmake_minimum_required(VERSION 3.14)
project(StepMetricsProject)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(THIRD_PARTY_LIBS "${CMAKE_SOURCE_DIR}/3rdPartyLibs")
if (NOT DEFINED OPEN_CASCADE_DIR)
    if (WIN32)
        set(OPEN_CASCADE_DIR "${THIRD_PARTY_LIBS}/libpack/occt-install-msvc2019")
    endif()
endif()

add_subdirectory(cli_engine)
