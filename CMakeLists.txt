cmake_minimum_required(VERSION 3.24)
project(roc_windows LANGUAGES C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(ROC_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/vendor/roc-toolkit")
set(ROC_SRC "${ROC_ROOT}/src")
set(OPENFEC_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/vendor/openfec")
set(RAGEL_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/tools/ragel-6.10")

# Build a native Ragel generator from the official release archive.  Its release
# sources already contain the bootstrap parser, so no Unix autotools are needed.
file(GLOB RAGEL_SOURCES CONFIGURE_DEPENDS "${RAGEL_ROOT}/ragel/*.cpp")
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/ragel_include")
configure_file(ragel-config.h "${CMAKE_CURRENT_BINARY_DIR}/ragel_include/config.h" COPYONLY)
add_executable(ragel_generator ${RAGEL_SOURCES})
target_include_directories(ragel_generator PRIVATE
  "${CMAKE_CURRENT_BINARY_DIR}/ragel_include"
  "${RAGEL_ROOT}/aapl" "${RAGEL_ROOT}/ragel")

set(BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(LIBUV_BUILD_SHARED OFF CACHE BOOL "" FORCE)
set(LIBUV_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(LIBUV_BUILD_BENCH OFF CACHE BOOL "" FORCE)
add_subdirectory(vendor/libuv EXCLUDE_FROM_ALL)

# OpenFEC's upstream top-level project also builds examples and tools.  Roc only
# needs the library, so keep this target narrow and generate its feature header
# in the build tree.
set(OF_USE_REED_SOLOMON_CODEC ON)
set(OF_USE_REED_SOLOMON_2_M_CODEC ON)
set(OF_USE_LDPC_STAIRCASE_CODEC ON)
set(OF_USE_2D_PARITY_MATRIX_CODEC ON)
set(OF_USE_LDPC_FROM_FILE_CODEC OFF)
set(ASSEMBLY_SSE_OPT OFF)
configure_file(
  "${OPENFEC_ROOT}/src/lib_common/of_build_config.h.in"
  "${OPENFEC_ROOT}/src/lib_common/of_build_config.h")
file(GLOB_RECURSE OPENFEC_SOURCES CONFIGURE_DEPENDS "${OPENFEC_ROOT}/src/*.c")
add_library(openfec_static STATIC ${OPENFEC_SOURCES})
target_compile_definitions(openfec_static PRIVATE
  OPENFEC_LITTLE_ENDIAN "__uint64=unsigned long long")
target_include_directories(openfec_static PUBLIC
  "${OPENFEC_ROOT}/src/lib_common")

set(ROC_MODULES
  roc_core roc_status roc_address roc_packet roc_audio roc_rtp roc_rtcp
  roc_fec roc_netio roc_sndio roc_pipeline roc_sdp roc_ctl roc_node)
set(ROC_TARGETS
  target_pc target_windows target_c11 target_libuv target_openfec
  target_nocsprng target_nodemangle target_nobacktrace)

set(ROC_TARGET_INCLUDE_DIRS)
foreach(module IN LISTS ROC_MODULES)
  foreach(target IN LISTS ROC_TARGETS)
    if(IS_DIRECTORY "${ROC_SRC}/internal_modules/${module}/${target}")
      list(APPEND ROC_TARGET_INCLUDE_DIRS
        "${ROC_SRC}/internal_modules/${module}/${target}")
    endif()
  endforeach()
endforeach()

set(ROC_OBJECTS)
foreach(module IN LISTS ROC_MODULES)
  set(module_dir "${ROC_SRC}/internal_modules/${module}")
  file(GLOB module_sources CONFIGURE_DEPENDS "${module_dir}/*.cpp")
  file(GLOB module_ragel CONFIGURE_DEPENDS "${module_dir}/*.rl")
  foreach(target IN LISTS ROC_TARGETS)
    file(GLOB target_sources CONFIGURE_DEPENDS
      "${module_dir}/${target}/${module}/*.cpp")
    list(APPEND module_sources ${target_sources})
  endforeach()

  foreach(rl IN LISTS module_ragel)
    get_filename_component(stem "${rl}" NAME_WE)
    set(generated "${CMAKE_CURRENT_BINARY_DIR}/generated/${module}/${stem}.cpp")
    add_custom_command(
      OUTPUT "${generated}"
      COMMAND ${CMAKE_COMMAND} -E make_directory "${CMAKE_CURRENT_BINARY_DIR}/generated/${module}"
      COMMAND ragel_generator -o "${generated}" "${rl}"
      DEPENDS ragel_generator "${rl}"
      VERBATIM)
    list(APPEND module_sources "${generated}")
  endforeach()

  add_library(${module}_objects OBJECT ${module_sources})
  target_compile_definitions(${module}_objects PRIVATE
    ROC_MODULE=${module}
    ROC_TARGET_PC ROC_TARGET_WINDOWS ROC_TARGET_C11 ROC_TARGET_LIBUV
    ROC_TARGET_OPENFEC ROC_TARGET_NOCSPRNG ROC_TARGET_NODEMANGLE
    ROC_TARGET_NOBACKTRACE __STDC_LIMIT_MACROS=1 _WIN32_WINNT=0x0A00)
  target_compile_options(${module}_objects PRIVATE
    $<$<COMPILE_LANGUAGE:CXX>:-fvisibility=hidden>)
  target_include_directories(${module}_objects PRIVATE
    "${ROC_SRC}/internal_modules" ${ROC_TARGET_INCLUDE_DIRS}
    "${ROC_ROOT}/3rdparty/hedley" "${ROC_ROOT}/3rdparty/dr_wav"
    "${CMAKE_CURRENT_SOURCE_DIR}/vendor/libuv/include"
    "${OPENFEC_ROOT}/src/lib_common")
  list(APPEND ROC_OBJECTS $<TARGET_OBJECTS:${module}_objects>)
endforeach()

file(GLOB ROC_API_SOURCES CONFIGURE_DEPENDS "${ROC_SRC}/public_api/src/*.cpp")
add_library(roc SHARED ${ROC_API_SOURCES} ${ROC_OBJECTS})
target_compile_definitions(roc PRIVATE
  ROC_MODULE=roc_api
  ROC_TARGET_PC ROC_TARGET_WINDOWS ROC_TARGET_C11 ROC_TARGET_LIBUV
  ROC_TARGET_OPENFEC ROC_TARGET_NOCSPRNG ROC_TARGET_NODEMANGLE
  ROC_TARGET_NOBACKTRACE __STDC_LIMIT_MACROS=1 _WIN32_WINNT=0x0A00)
target_include_directories(roc PRIVATE
  "${ROC_SRC}/internal_modules" "${ROC_SRC}/public_api/include"
  ${ROC_TARGET_INCLUDE_DIRS}
  "${ROC_ROOT}/3rdparty/hedley" "${ROC_ROOT}/3rdparty/dr_wav"
  "${CMAKE_CURRENT_SOURCE_DIR}/vendor/libuv/include"
  "${OPENFEC_ROOT}/src/lib_common")
target_link_libraries(roc PRIVATE uv_a openfec_static ws2_32 iphlpapi userenv dbghelp winmm)
set_target_properties(roc PROPERTIES
  PREFIX ""
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/Release"
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/Release")
get_filename_component(TOOLCHAIN_BIN "${CMAKE_CXX_COMPILER}" DIRECTORY)
add_custom_command(TARGET roc POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
    "${TOOLCHAIN_BIN}/libc++.dll" "$<TARGET_FILE_DIR:roc>"
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
    "${TOOLCHAIN_BIN}/libunwind.dll" "$<TARGET_FILE_DIR:roc>"
  VERBATIM)
