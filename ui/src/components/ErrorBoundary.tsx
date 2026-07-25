import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';

/**
 * The last line of defense against a blank screen.
 *
 * Fresh-user BLOCK-3: a canvas node with no `position` made ReactFlow throw
 * inside `setNodes`, React unmounted the whole tree, and odin rendered a solid
 * black page — no message, no recovery affordance, the failure visible only in
 * the browser console. A tool that reports a failed `tofu apply` honestly must
 * not render nothing when its own UI breaks.
 *
 * React gives exactly one hook for this, and it is class-only.
 */
type Props = { children: ReactNode };
type State = { error: Error | null };

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('odin UI crashed:', error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="min-h-screen bg-[#050508] text-text-primary font-mono p-8 flex flex-col gap-4">
        <div className="text-neon-red text-sm tracking-widest">ODIN UI CRASHED</div>
        <div className="text-xs text-text-secondary max-w-3xl leading-relaxed">
          The interface stopped rendering. Nothing you have applied is affected — this is the
          browser half only, and your canvas is still on disk at <code>.odin/canvas.json</code>.
        </div>
        <pre className="text-xs text-neon-red border border-neon-red p-3 overflow-x-auto whitespace-pre-wrap">
          {error.message}
        </pre>
        <div className="text-xs text-text-secondary max-w-3xl leading-relaxed">
          Most common cause: a canvas saved outside the UI whose nodes are missing required
          fields. Check it with <code>odin canvas get -o json</code>; every node needs
          {' '}<code>id</code>, <code>type</code>, <code>position</code> and <code>data.label</code>.
        </div>
        <button
          className="self-start border border-border-bright px-3 py-2 text-xs hover:bg-bg-secondary"
          onClick={() => window.location.reload()}
        >
          reload
        </button>
      </div>
    );
  }
}
