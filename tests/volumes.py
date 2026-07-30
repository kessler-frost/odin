"""A fake that has docker's VOLUME LABEL semantics, for every test whose
subject is the reclaim.

The reclaim's entire safety argument is "the `odin.env` label is exact, a name
is not" (`runtime/colima.py::ENV_LABEL`). A fake `volume_names` that ignored its
`env` argument and returned everything would make that argument untestable --
worse, it would make a driver that FORGOT the filter pass, which is the shape of
the four guards in this repo that silently never fired. So this models the two
things docker really does:

  * `docker volume ls --filter label=odin=1` lists every odin volume;
  * adding `--filter label=odin.env=<env>` narrows it to volumes whose label
    matches EXACTLY -- a volume with no such label is absent from every narrowed
    answer, which is precisely why `odin env rm` cannot reach a volume created
    before v0.8.15.

`refuse` is the other half: docker refuses to remove a volume a container still
references (probed on the real CLI -- `rc 1: remove <vol>: volume is in use -
[<container id>]`), and that refusal is the guard working. Tests use it to make
the guard FIRE rather than to assert around it.
"""
from __future__ import annotations

# Docker's own wording, so a test asserting on what a user is shown is asserting
# on the real sentence rather than one this repo invented.
IN_USE = "docker volume rm failed (exit 1): remove {name}: volume is in use - [c0ffee1234]"


class FakeVolumes:
    """Mixin. Lazily allocated so a `@dataclass` fake can pick it up without a
    constructor of its own -- five fakes across this suite already exist, and
    none of them should have to grow one to gain volume support."""

    @property
    def _labels(self) -> dict[str, str | None]:
        """volume name -> its `odin.env` label, or None for an unlabelled one."""
        if not hasattr(self, "_volume_labels"):
            self._volume_labels: dict[str, str | None] = {}
        return self._volume_labels

    @property
    def _refused(self) -> set[str]:
        if not hasattr(self, "_refused_volumes"):
            self._refused_volumes: set[str] = set()
        return self._refused_volumes

    # --- the seams a test drives ------------------------------------------

    def seed_volume(self, name: str, env: str | None) -> FakeVolumes:
        """Put a volume on the machine directly. `env=None` is an UNLABELLED
        one: what odin made before v0.8.15, and what a bare `-v name:/path`
        auto-creates."""
        self._labels[name] = env
        return self

    def refuse_volume(self, name: str) -> FakeVolumes:
        """Make docker refuse to remove this one, as an attached container does."""
        self._refused.add(name)
        return self

    @property
    def volumes(self) -> set[str]:
        """Every volume on the fake machine -- what a test asserts survived."""
        return set(self._labels)

    # --- the RuntimeDriver surface ---------------------------------------

    async def create_volume(self, name: str, env: str) -> None:
        self._labels[name] = env

    async def remove_volume(self, name: str) -> None:
        if name in self._refused:
            raise RuntimeError(IN_USE.format(name=name))
        self._labels.pop(name, None)

    async def volume_names(self, env: str | None = None) -> list[str]:
        return sorted(
            name for name, label in self._labels.items()
            if env is None or label == env
        )


class BlindVolumes(FakeVolumes):
    """A machine odin cannot ask about volumes at all -- the `unknown` case,
    which must never wear the same words as "there are none"."""

    async def volume_names(self, env: str | None = None) -> list[str]:
        raise RuntimeError("Cannot connect to the Docker daemon")
