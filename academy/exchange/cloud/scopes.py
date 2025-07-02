from __future__ import annotations

from globus_sdk.scopes import ScopeBuilder

AcademyExchangeScopes = ScopeBuilder(
    # "Academy Exchange Server" application client ID
    'a7e16357-8edf-414d-9e73-85e4b0b18be4',
    # The academy_exchange scope has scope ID:
    #   17619205-054c-4829-a1a8-f4b6968c76d2
    known_url_scopes=['academy_exchange'],
)
