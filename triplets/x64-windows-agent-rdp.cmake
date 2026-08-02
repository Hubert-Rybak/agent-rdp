# The upstream vcpkg FreeRDP port explicitly disables its unmaintained native
# Windows client. agent-rdp needs wfreerdp.exe, so opt it back in for this
# application-specific triplet. vcpkg appends triplet configure options after
# port options, making this ON override the port's WITH_CLIENT_WINDOWS=OFF.
set(VCPKG_TARGET_ARCHITECTURE x64)
set(VCPKG_CRT_LINKAGE dynamic)
set(VCPKG_LIBRARY_LINKAGE dynamic)
set(VCPKG_PROVIDED_FORTRAN ON)
set(VCPKG_CMAKE_CONFIGURE_OPTIONS "-DWITH_CLIENT_WINDOWS=ON")
