/**
 * The connection edge, and the picker it finally makes real.
 *
 * ## What was wrong
 *
 * `rds -> ecs` is the most-drawn line in any architecture diagram, and until
 * v0.8.15 odin did nothing with it. It produced a cyan IAM edge whose default
 * grant was `rds-db:connect` -- an action `gateway/classify.py` can NEVER emit,
 * because it builds `rds:<Action>` from the query protocol's `Action` param and
 * `rds-db:` is a different service prefix entirely. The vocabulary test even
 * whitelisted it (`NOT_API_ACTIONS`) as "not an API call", which was true and
 * was not the same thing as enforceable. For elasticache the default was
 * Describe-only, and `iam.ts` already stated plainly that no IAM policy can gate
 * a Redis GET/SET.
 *
 * So the most-drawn line was decorative.
 *
 * "Connection" turned out to be three mechanisms, and only one was missing:
 * reachability is the `sg` edge and is real; permission is the `iam` edge and is
 * real where the data plane is AWS-signed; and the ADDRESS -- typing
 * `${{db.DATABASE_URL}}` into the consumer's env by hand -- was real too and had
 * no gesture. This is that gesture.
 */
import { describe, expect, it } from 'bun:test';

import {
  UNMODELLED,
  computeTypes,
  connectionConsumerTypes,
  connectionRefs,
  defaultPermissions,
  defaultPermissionsFor,
  detectEdgeTypes,
  edgeDataForConnection,
  edgeStyle,
  edgeTypeChoices,
  edgeTypes,
  iamActionsForTarget,
  iamTargetTypes,
  includesEdgeType,
  parseEdgeTypes,
  primaryEdgeType,
  selectedEdgeTypes,
  serializeEdgeTypes,
  toggleEdgeType,
} from './iam';

describe('the connection edge', () => {
  it('is what rds -> ecs means FIRST, ahead of the grant', () => {
    // The order is the point: `detectEdgeTypes[0]` is what a fresh drag writes,
    // and what a user drawing this line wants is the connection string, not a
    // control-plane permission most apps never call.
    expect(detectEdgeTypes('rds', 'ecs')).toEqual(['connection', 'iam']);
    expect(edgeDataForConnection('rds', 'ecs').edgeType).toBe('connection');
  });

  it('reads the same either way round', () => {
    expect(detectEdgeTypes('ecs', 'rds')).toEqual(detectEdgeTypes('rds', 'ecs'));
    expect(edgeDataForConnection('ecs', 'rds')).toEqual(edgeDataForConnection('rds', 'ecs'));
  });

  it('grants NOTHING — connecting to a Postgres container consults no IAM', () => {
    // The honest answer to "what permissions does a connection require". Real
    // AWS would say `rds-db:connect`, and odin does not implement IAM database
    // authentication at all: the container takes the password out of
    // DATABASE_URL and asks nobody.
    expect(edgeDataForConnection('rds', 'ecs').permissions).toEqual([]);
    expect(defaultPermissionsFor('connection', 'rds', 'ecs')).toEqual([]);
  });

  it('covers both cache and database, for both container consumers', () => {
    for (const producer of Object.keys(connectionRefs)) {
      for (const consumer of connectionConsumerTypes) {
        expect(detectEdgeTypes(producer, consumer)).toContain('connection');
      }
    }
    expect(Object.keys(connectionRefs).sort()).toEqual(['elasticache', 'rds']);
  });

  it('names the variable a user would have typed, and the fact it reads', () => {
    // These two strings are the whole contract with `gateway/wiring.py`:
    // `DATABASE_URL` is what the app expects to find in its environment, and the
    // attr is the key `producer_facts` publishes. A rename on either side
    // produces a ref that resolves to nothing and a task that STOPS.
    expect(connectionRefs.rds).toEqual({ var: 'DATABASE_URL', attr: 'DATABASE_URL' });
    expect(connectionRefs.elasticache).toEqual({ var: 'REDIS_URL', attr: 'REDIS_URL' });
  });

  it('does NOT cover ec2 — that container never receives the env map', () => {
    // MEASURED, not assumed: `gateway/wiring.py::node_env` has exactly two
    // callers, `gateway/models/ecsctl.py` and `gateway/models/lambdactl.py`.
    // `gateway/models/ec2compute.py` imports `workload_env` (the issued gateway
    // credentials) and never `node_env`, so a ref authored onto an ec2 node
    // reaches NOTHING -- the drawn-line-that-does-nothing bug this edge type
    // exists to fix. It stays IAM-only, and `docs/limits.md` says so.
    expect([...connectionConsumerTypes].sort()).toEqual(['ecs', 'lambda']);
    expect(detectEdgeTypes('rds', 'ec2')).toEqual(['iam']);
    expect(detectEdgeTypes('elasticache', 'ec2')).toEqual(['iam']);
  });

  it('does not cover alb or ecr, which publish facts with no obvious var name', () => {
    // Both ARE in `spec/models.py::REFERENCEABLE_KINDS` and both publish a
    // wiring fact (ALB_ENDPOINT, REPOSITORY_URI). Neither has a single obvious
    // environment-variable name, and guessing one authors a field the app does
    // not read -- the same bug from the other direction.
    expect(detectEdgeTypes('alb', 'lambda')).toEqual([UNMODELLED]);
    expect(detectEdgeTypes('ecr', 'lambda')).toEqual(['iam']);
  });

  it('has a definition, so it renders as itself rather than a grey fallback', () => {
    expect(edgeTypes.connection).toBeDefined();
    expect(edgeTypes.connection.label).toBe('Connection');
    expect(edgeStyle('connection').stroke).toBe(edgeTypes.connection.color);
    expect(edgeStyle('connection').stroke).not.toBe(edgeTypes[UNMODELLED].color);
  });
});

describe('who the principal is, when a producer is also an IAM target', () => {
  // FOUND BY THIS FILE, not by reading it. Pairing `rds` with `ecs` made two
  // IAM targets face each other for the first time, and the old tie-break asked
  // the DESTINATION end first -- so `rds -> ecs` answered `ecs:RunTask` and
  // odin offered to grant a DATABASE permission to run ECS tasks.
  it('grants the workload access to the database, not the reverse', () => {
    expect(defaultPermissionsFor('iam', 'rds', 'ecs')).toEqual(defaultPermissions.rds);
    expect(defaultPermissionsFor('iam', 'ecs', 'rds')).toEqual(defaultPermissions.rds);
    expect(defaultPermissionsFor('iam', 'rds', 'ecs')).not.toEqual(defaultPermissions.ecs);
  });

  it('holds for the cache too, and for the lambda consumer', () => {
    expect(defaultPermissionsFor('iam', 'elasticache', 'ecs')).toEqual(defaultPermissions.elasticache);
    expect(defaultPermissionsFor('iam', 'lambda', 'rds')).toEqual(defaultPermissions.rds);
    expect(defaultPermissionsFor('iam', 'rds', 'lambda')).toEqual(defaultPermissions.rds);
  });

  it('the two orderings of every REAL iam pair agree, unless both ends are compute', () => {
    // The generalisation, and the property the old rule quietly broke. Direction
    // may only decide when it is the ONLY thing that can say who calls whom --
    // i.e. when both ends can hold a role. Anywhere else the user drew the same
    // intent either way and must get the same answer.
    //
    // Scoped to pairs the REGISTRY actually calls `iam`, which is the only input
    // the product ever gives this function. Two non-compute kinds (`s3` and
    // `dynamodb`, say) are `unmodelled` and never reach it; asserting over them
    // would pin an answer no user can produce -- a test agreeing with itself
    // rather than with the thing being tested.
    const kinds = [...new Set([...computeTypes, ...iamTargetTypes])];
    const iamPairs = kinds.flatMap((a) => kinds
      .filter((b) => detectEdgeTypes(a, b).includes('iam'))
      .map((b): [string, string] => [a, b]));
    expect(iamPairs.length).toBeGreaterThan(20);   // guards the guard

    const disagreeing = iamPairs
      .filter(([a, b]) => !(computeTypes.has(a) && computeTypes.has(b)))
      .filter(([a, b]) => defaultPermissionsFor('iam', a, b).join() !== defaultPermissionsFor('iam', b, a).join())
      .map(([a, b]) => `${a}/${b}`);
    expect(disagreeing).toEqual([]);
  });
});

describe('the two decorative DEFAULTS this replaces', () => {
  // The distinction that took two agents disagreeing to get right. `edge-registry`
  // drew it for ecr; this file had deleted the actions outright, which was worse.
  //
  // TICKABLE and DEFAULT are different promises. A drawn permission becomes a real
  // `aws_iam_role_policy`, and the generated Terraform is meant to be portable --
  // taken to Amazon, `rds-db:connect` is exactly what IAM DB auth needs. A
  // DEFAULT is what odin ticks FOR you, and ticking something odin cannot enforce
  // is odin claiming a protection it has not got. So the fix is to un-tick, not
  // to delete.
  it('rds no longer DEFAULTS to a grant the gateway cannot evaluate', () => {
    // `classify.py` emits `rds:<Action>` and nothing else, so no request can
    // ever carry `rds-db:connect` to `evaluate`.
    expect(defaultPermissions.rds).toEqual(['rds:DescribeDBInstances']);
    expect(defaultPermissions.rds).not.toContain('rds-db:connect');
  });

  it('...but keeps it TICKABLE, because the generated file has to be portable', () => {
    // Deleting it would make odin's Terraform unable to express a thing real AWS
    // does. The honest position is to offer it and not pre-tick it.
    expect(iamActionsForTarget.rds).toContain('rds-db:connect');
  });

  it('nothing odin ticks FOR you is in a prefix the classifier cannot emit', () => {
    // The generalisation, and the shape rather than the instance: a DEFAULT must
    // always be enforceable, whatever service it belongs to. `rds-db:` is the
    // only unclassifiable prefix in the vocabulary today, and this catches the
    // next one too.
    const defaulted = Object.values(defaultPermissions).flat();
    expect(defaulted.filter((a) => a.startsWith('rds-db:'))).toEqual([]);
  });

  it('ecr defaults to the one op the gateway has a handler for', () => {
    // `gateway/models/ecr.py::_HANDLERS` answers seven ops, and the three
    // image-layer verbs are in none of them -- they are the DATA plane, which
    // `docker push`/`pull` reaches on the registry:2 container's own published
    // port, never through the gateway. They stay offered and stay un-ticked;
    // `tests/gateway/test_ecr_vocabulary_has_handlers.py` is the Python half.
    expect(defaultPermissions.ecr).toEqual(['ecr:GetAuthorizationToken']);
    expect(iamActionsForTarget.ecr).toContain('ecr:BatchGetImage');
  });
});

describe('one line, more than one meaning', () => {
  it('a single meaning serialises to exactly the bytes it always did', () => {
    // The reason this needed no migration: `"iam"` in, `"iam"` out, and every
    // canvas ever saved carries a value with no separator in it.
    expect(serializeEdgeTypes(['iam'], ['iam'])).toBe('iam');
    expect(parseEdgeTypes('iam')).toEqual(['iam']);
    expect(parseEdgeTypes('network')).toEqual(['network']);
  });

  it('joins in REGISTRY order, never click order', () => {
    // Two users ticking the same boxes must produce the same string, or the
    // canvas diffs for nothing and every save is a spurious revision.
    const available = ['connection', 'iam'];
    expect(serializeEdgeTypes(['iam', 'connection'], available)).toBe('connection+iam');
    expect(serializeEdgeTypes(['connection', 'iam'], available)).toBe('connection+iam');
  });

  it('styles a joined value as its PRIMARY meaning instead of falling to grey', () => {
    // A bare `edgeTypes['connection+iam']` lookup misses, and the line would
    // draw grey -- rendering as "not modelled" while meaning two things.
    expect(primaryEdgeType('connection+iam')).toBe('connection');
    expect(edgeStyle('connection+iam').stroke).toBe(edgeTypes.connection.color);
    expect(edgeStyle('connection+iam').stroke).not.toBe(edgeTypes[UNMODELLED].color);
  });

  it('still sees the grant inside a joined value', () => {
    // Every `edgeType === 'iam'` in `Canvas.tsx` had to become this. Comparing
    // the whole string would hide the permission label on the line while
    // `gateway/policy.py` went on enforcing the policy -- the screen saying one
    // thing and the engine doing another.
    expect(includesEdgeType('connection+iam', 'iam')).toBe(true);
    expect(includesEdgeType('connection', 'iam')).toBe(false);
    expect(includesEdgeType('iam', 'iam')).toBe(true);
  });

  it('is total: junk, empty and null all resolve rather than throwing', () => {
    expect(parseEdgeTypes(undefined)).toEqual([]);
    expect(parseEdgeTypes('')).toEqual([]);
    expect(parseEdgeTypes('+ + ')).toEqual([]);
    expect(parseEdgeTypes('iam++iam')).toEqual(['iam']);
    expect(primaryEdgeType(undefined)).toBe(UNMODELLED);
    expect(edgeStyle('').stroke).toBe(edgeTypes[UNMODELLED].color);
  });
});

describe('the picker, which had never once rendered', () => {
  // `ConfigPanel.tsx`'s `<select>` was gated on `availableTypes.length > 1` and
  // every pair meant exactly one thing until v0.8.15, so not one line of it had
  // ever run in production. It is unproven code, and this is the proof.
  const available = detectEdgeTypes('rds', 'ecs'); // ['connection', 'iam']

  it('opens for exactly the pairs that have a choice, and not otherwise', () => {
    expect(edgeTypeChoices('connection', available).length).toBeGreaterThan(1);
    expect(edgeTypeChoices('iam', detectEdgeTypes('ec2', 's3')).length).toBe(1);
  });

  it('shows the default when the edge stores nothing', () => {
    expect(selectedEdgeTypes(undefined, available)).toEqual(['connection']);
    expect(selectedEdgeTypes('', available)).toEqual(['connection']);
  });

  it('honours a stored value VERBATIM, even one the pair no longer suggests', () => {
    // Load-bearing, not tidiness. An `iam` edge compiles to a real policy
    // whichever pair it sits on, because `gateway/policy.py` reads the edge's
    // kind and not the kinds of its endpoints. Filtering the stored value
    // against the registry would show no permissions for a grant the gateway is
    // still enforcing.
    expect(selectedEdgeTypes('iam', detectEdgeTypes('sns', 'sqs'))).toEqual(['iam']);
    expect(edgeTypeChoices('iam', detectEdgeTypes('sns', 'sqs'))).toEqual(['subscription', 'iam']);
  });

  it('ticking a second meaning KEEPS the first — it is not a radio button', () => {
    // In AWS the meanings co-occur: a workload wired to a database may also call
    // its control plane, exactly as an event source mapping needs the role to
    // hold `sqs:ReceiveMessage`.
    const next = toggleEdgeType('connection', available, 'iam', true, 'rds', 'ecs');
    expect(next.edgeType).toBe('connection+iam');
  });

  it('ticking IAM auto-ticks the permissions that meaning requires', () => {
    // An `iam` edge with no permissions is not harmless: `agent/hcl.py` reserves
    // a role for it and emits an `aws_iam_role` carrying no policy at all.
    const next = toggleEdgeType('connection', available, 'iam', true, 'rds', 'ecs');
    expect(next.permissions).toEqual(defaultPermissions.rds);
    expect(next.permissions.length).toBeGreaterThan(0);
  });

  it('does not RE-seed permissions the user has already edited', () => {
    // Ticking `connection` on an edge that is already IAM must not silently put
    // back a permission the user deliberately unticked.
    const edited = ['rds:*'];
    const next = toggleEdgeType('iam', available, 'connection', true, 'rds', 'ecs', edited);
    expect(next.edgeType).toBe('connection+iam');
    expect(next.permissions).toEqual(edited);
  });

  it('unticking IAM clears the permissions with it', () => {
    // `permissions` on a non-`iam` edge is read by nothing on the Python side
    // (`compile_policies` and `hcl.py::_granted_ids` both gate on the kind), so
    // leaving them would show ticks that grant nothing.
    const next = toggleEdgeType('connection+iam', available, 'iam', false, 'rds', 'ecs', ['rds:*']);
    expect(next.edgeType).toBe('connection');
    expect(next.permissions).toEqual([]);
  });

  it('the LAST remaining meaning cannot be unticked', () => {
    // An edge with no meaning reloads as `unmodelled` and silently loses the
    // choice. The panel disables that box rather than letting the click look
    // like it did something.
    const next = toggleEdgeType('connection', available, 'connection', false, 'rds', 'ecs');
    expect(next.edgeType).toBe('connection');
    expect(parseEdgeTypes(next.edgeType).length).toBe(1);
  });

  it('round-trips: tick, untick, and you are back where you started', () => {
    const on = toggleEdgeType('connection', available, 'iam', true, 'rds', 'ecs');
    const off = toggleEdgeType(on.edgeType, available, 'iam', false, 'rds', 'ecs', on.permissions);
    expect(off).toEqual({ edgeType: 'connection', permissions: [] });
  });
});
