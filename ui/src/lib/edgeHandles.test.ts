import { describe, expect, it } from 'bun:test';
import { centreOf, facingHandles, resolveHandles } from './edgeHandles';

describe('facingHandles: the sides two nodes face each other on', () => {
  it('joins right -> left when the target is to the right', () => {
    expect(facingHandles({ x: 0, y: 0 }, { x: 500, y: 0 })).toEqual({
      sourceHandle: 'right', targetHandle: 'left',
    });
  });

  it('joins left -> right when the target is to the left', () => {
    expect(facingHandles({ x: 500, y: 0 }, { x: 0, y: 0 })).toEqual({
      sourceHandle: 'left', targetHandle: 'right',
    });
  });

  it('joins bottom -> top when the target is mostly below', () => {
    expect(facingHandles({ x: 0, y: 0 }, { x: 20, y: 400 })).toEqual({
      sourceHandle: 'bottom', targetHandle: 'top',
    });
  });

  it('joins top -> bottom when the target is mostly above', () => {
    expect(facingHandles({ x: 0, y: 400 }, { x: 20, y: 0 })).toEqual({
      sourceHandle: 'top', targetHandle: 'bottom',
    });
  });

  it('breaks a tie horizontally, and is total', () => {
    // odin's own drop placement lays resources out in a row, so horizontal is
    // the right tie-break — and every input must get an answer.
    expect(facingHandles({ x: 0, y: 0 }, { x: 100, y: 100 }).sourceHandle).toBe('right');
    expect(facingHandles({ x: 0, y: 0 }, { x: 0, y: 0 }).sourceHandle).toBe('right');
  });
});

describe('centreOf', () => {
  it('offsets by half the size when a size is known', () => {
    expect(centreOf({ position: { x: 100, y: 200 }, size: { width: 200, height: 40 } }))
      .toEqual({ x: 200, y: 220 });
  });

  it('falls back to the position when size is absent', () => {
    // A canvas that omits handles usually omits `size` too; the position alone
    // is still far better than an arbitrary handle.
    expect(centreOf({ position: { x: 100, y: 200 } })).toEqual({ x: 100, y: 200 });
    expect(centreOf({})).toEqual({ x: 0, y: 0 });
  });
});

describe('resolveHandles: infer only what the author did not say', () => {
  const left = { x: 0, y: 0 };
  const right = { x: 600, y: 0 };

  it('infers both when the edge names neither', () => {
    // The measured bug: an EC2 -> S3 edge with no handles routed backwards and
    // put its permission label at x=601, inside the source node.
    expect(resolveHandles({}, left, right)).toEqual({ sourceHandle: 'right', targetHandle: 'left' });
  });

  it('never second-guesses handles the user drew', () => {
    const drawn = { sourceHandle: 'top', targetHandle: 'bottom' };
    expect(resolveHandles(drawn, left, right)).toEqual(drawn);
  });

  it('keeps the half a partially-specified edge named', () => {
    expect(resolveHandles({ sourceHandle: 'bottom' }, left, right))
      .toEqual({ sourceHandle: 'bottom', targetHandle: 'left' });
    expect(resolveHandles({ targetHandle: 'top' }, left, right))
      .toEqual({ sourceHandle: 'right', targetHandle: 'top' });
  });

  it('invents nothing for an edge whose endpoints are not on the canvas', () => {
    // Such an edge cannot be drawn at all, so a guessed side would be a guess
    // about nothing.
    expect(resolveHandles({}, null, right)).toEqual({ sourceHandle: null, targetHandle: null });
    expect(resolveHandles({}, left, null)).toEqual({ sourceHandle: null, targetHandle: null });
  });

  it('treats an explicit null the same as absent', () => {
    expect(resolveHandles({ sourceHandle: null, targetHandle: null }, left, right))
      .toEqual({ sourceHandle: 'right', targetHandle: 'left' });
  });
});
