# SPDX-FileCopyrightText: 2014 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

set(INC
  .
  intern
  ../makesrna
)

set(INC_SYS
)

set(SRC
  intern/SIM_mass_spring.cc
  intern/hair_volume.cc
  intern/implicit_blender.cc
  intern/implicit_eigen.cc

  intern/ConstrainedConjugateGradient.h
  intern/eigen_utils.h
  intern/implicit.h

  SIM_mass_spring.h
)

set(LIB
  PRIVATE bf::blenkernel
  PRIVATE bf::blenlib
  PRIVATE bf::depsgraph
  PRIVATE bf::dna
  PRIVATE bf::functions
  PRIVATE bf::imbuf
  PRIVATE bf::intern::guardedalloc
  PRIVATE bf::nodes
  PRIVATE bf::dependencies::eigen
)


blender_add_lib(bf_simulation "${SRC}" "${INC}" "${INC_SYS}" "${LIB}")

if(WITH_PYTHON)
  add_library(xblender_gel_kernels SHARED intern/gel_kernels.cc)
  target_link_libraries(xblender_gel_kernels PRIVATE bf::dependencies::eigen)
  set_target_properties(xblender_gel_kernels PROPERTIES PREFIX "")
  if(APPLE)
    set_target_properties(xblender_gel_kernels PROPERTIES
      XCODE_ATTRIBUTE_SKIP_INSTALL "YES"
      XCODE_ATTRIBUTE_INSTALL_PATH ""
    )
  endif()
endif()
