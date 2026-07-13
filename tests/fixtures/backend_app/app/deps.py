from typing import Annotated

from fastapi import Depends

from .auth import get_current_user

# auth type alias used across files (6.3 cross-file resolution)
CurrentUser = Annotated[dict, Depends(get_current_user)]
