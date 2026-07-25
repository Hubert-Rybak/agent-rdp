cmake_minimum_required(VERSION 3.20)
project(rdp2exec LANGUAGES CXX)

if(NOT WIN32)
  message(FATAL_ERROR "rdp2exec (Windows-to-Windows edition) only builds for Windows targets. "
                       "Configure with a Windows toolchain, e.g. via vcpkg's mingw-x64-windows "
                       "or an MSVC generator.")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(PkgConfig REQUIRED)
pkg_check_modules(FREERDP REQUIRED IMPORTED_TARGET freerdp3)
pkg_check_modules(FREERDP_CLIENT REQUIRED IMPORTED_TARGET freerdp-client3)
pkg_check_modules(WINPR REQUIRED IMPORTED_TARGET winpr3)

# --- Client-side FreeRDP DVC plugin (runs on the Windows agent/orchestrator host) ---
add_library(rdp2exec_client SHARED
  src/plugin/rdp2exec_client.cpp
)
target_include_directories(rdp2exec_client PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(rdp2exec_client PRIVATE
  PkgConfig::FREERDP
  PkgConfig::FREERDP_CLIENT
  PkgConfig::WINPR
  ws2_32
)
set_target_properties(rdp2exec_client PROPERTIES
  OUTPUT_NAME "rdp2exec-client"
  PREFIX ""
)

# --- Windows target-side bridge (runs on the remote RDP host, off the redirected drive) ---
add_executable(rdp2exec_bridge
  src/windows/rdp2exec_bridge.cpp
)
target_include_directories(rdp2exec_bridge PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_compile_definitions(rdp2exec_bridge PRIVATE _WIN32_WINNT=0x0A00 UNICODE _UNICODE)
# /municode via the "wmain" entry point: request a Unicode CRT entry point.
if(MSVC)
  set_target_properties(rdp2exec_bridge PROPERTIES LINK_FLAGS "/ENTRY:wmainCRTStartup")
else()
  target_link_options(rdp2exec_bridge PRIVATE -municode)
endif()
target_link_libraries(rdp2exec_bridge PRIVATE
  wtsapi32
  user32
  advapi32
  shell32
)

# On Windows a SHARED library's loadable output (the .dll) is a RUNTIME
# artifact, not LIBRARY (LIBRARY only applies to Unix .so); ARCHIVE picks up
# the accompanying .lib import library.
install(TARGETS rdp2exec_client rdp2exec_bridge
  RUNTIME DESTINATION artifacts
  ARCHIVE DESTINATION artifacts
)
