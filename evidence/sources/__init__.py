"""evidence/sources — one adapter per permitted source (DESIGN_54 §3). Every adapter returns an AdapterResult: items plus every
fetch attempt (successful or not) and never raises past its own boundary. Robinhood is not a source and has no adapter."""
