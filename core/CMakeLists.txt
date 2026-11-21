# PocketPear's native CPU core: libppcpu (dynarmic, A32 guest). DYNARMIC_DIR points at a dynarmic
# checkout with its externals (core/fetch-dynarmic.sh makes one); it is not part of this repo.
cmake_minimum_required(VERSION 3.16)
project(ppcpu CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

set(DYNARMIC_DIR "" CACHE PATH "dynarmic source checkout")
set(DYNARMIC_FRONTENDS "A32" CACHE STRING "" FORCE)
set(DYNARMIC_TESTS OFF CACHE BOOL "" FORCE)
set(DYNARMIC_USE_BUNDLED_EXTERNALS ON CACHE BOOL "" FORCE)
set(DYNARMIC_WARNINGS_AS_ERRORS OFF CACHE BOOL "" FORCE)
set(DYNARMIC_USE_PRECOMPILED_HEADERS OFF CACHE BOOL "" FORCE)
add_subdirectory(${DYNARMIC_DIR} dynarmic EXCLUDE_FROM_ALL)

add_library(ppcpu SHARED ppcpu.cpp)
target_link_libraries(ppcpu PRIVATE dynarmic merry::mcl)
target_include_directories(ppcpu PRIVATE ${DYNARMIC_DIR}/src ${Boost_INCLUDE_DIR})
set_target_properties(ppcpu PROPERTIES CXX_VISIBILITY_PRESET hidden)
