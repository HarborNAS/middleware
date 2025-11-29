<%
	buildtime = middleware.call_sync('system.build_time')
	motd = middleware.call_sync('system.advanced.config')['motd']
%>\

    HarborOS (c) 2025-${buildtime.year}, Harborinno Ltd. dba HarborOS
	All rights reserved.

	This software is a modified version of TrueNAS, originally
	released by iXsystems, Inc.

	Portions of this software are copyright (c) 2009-${buildtime.year},
	iXsystems, Inc. dba TrueNAS All rights reserved.

	Original TrueNAS code is released under the LGPLv3 and GPLv3 licenses
	with some source files copyrighted by (c) iXsystems, Inc. All other
	components are released under their own respective licenses.

	For more information, documentation, help or support, go here:
	https://harboros.ai/

Warning: the supported mechanisms for making configuration changes
are the HarborOS WebUI, CLI, and API exclusively. ALL OTHERS ARE
NOT SUPPORTED AND WILL RESULT IN UNDEFINED BEHAVIOR AND MAY
RESULT IN SYSTEM FAILURE.

% if motd:
${motd}
% endif
