"""Per-service gateway model modules (research-coverage §3: "the coverage
work generalizes synth.py from a handful of gap-fill handlers into
per-service model modules"). Each module owns create/describe/delete over a
per-env state store for a service that has NO backing container -- the
module IS the whole service, dispatched from `synth.pure_answer`."""
