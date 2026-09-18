# Routing fixtures

The `v1` fixtures are synthetic canonical `BuildingModel` inputs for the deterministic router:

- `normal.json`: unobstructed source-to-load route
- `alternate-obstacle.json`: hard obstacle forces an alternate path
- `impossible.json`: a hard obstacle covers a required corridor, so routing must fail explicitly

They contain no customer data and use only the canonical v1 model contract.
