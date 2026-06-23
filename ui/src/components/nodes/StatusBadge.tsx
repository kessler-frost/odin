// The real World phases (the retired validate/simulate vocabulary is gone).
const statusStyles: Record<string, string> = {
  draft: 'text-text-muted border-border bg-bg-tertiary',
  pending: 'text-text-muted border-border bg-bg-tertiary',
  starting: 'text-neon-yellow border-neon-yellow bg-[rgba(255,221,0,0.1)] animate-[pulse_1.5s_infinite]',
  queued: 'text-neon-yellow border-neon-yellow bg-[rgba(255,221,0,0.08)] animate-[pulse_2s_infinite]',
  blocked: 'text-neon-purple border-neon-purple bg-[rgba(170,85,255,0.1)] animate-[pulse_1.5s_infinite]',
  running: 'text-neon-blue border-neon-blue bg-[rgba(0,187,255,0.1)] animate-[pulse_1.5s_infinite]',
  healthy: 'text-neon-green border-neon-green bg-[rgba(0,255,136,0.1)] shadow-[0_0_8px_rgba(0,255,136,0.15)]',
  done: 'text-neon-green border-neon-green bg-[rgba(0,255,136,0.1)] shadow-[0_0_8px_rgba(0,255,136,0.15)]',
  crashed: 'text-neon-red border-neon-red bg-[rgba(255,51,85,0.1)] shadow-[0_0_8px_rgba(255,51,85,0.15)]',
  error: 'text-neon-red border-neon-red bg-[rgba(255,51,85,0.1)] shadow-[0_0_8px_rgba(255,51,85,0.15)]',
  evicted: 'text-text-muted border-border bg-bg-tertiary',
};

// Phase -> accent text color, reused (e.g. by the ConfigPanel multi-select summary)
// so the canvas and panel never disagree on what a phase looks like.
export const phaseTextColor: Record<string, string> = {
  healthy: 'text-neon-green',
  done: 'text-neon-green',
  crashed: 'text-neon-red',
  error: 'text-neon-red',
  blocked: 'text-neon-purple',
  starting: 'text-neon-yellow',
  queued: 'text-neon-yellow',
  running: 'text-neon-blue',
};

// Phases that carry a failure/wait reason worth surfacing on hover.
const WITH_REASON = ['error', 'crashed', 'blocked', 'evicted'];

export default function StatusBadge({ status, error }: { status: string; error?: string }) {
  const showReason = Boolean(error) && WITH_REASON.includes(status);
  return (
    <span
      title={showReason ? error : undefined}
      className={`font-mono text-[9px] py-0.5 px-1.5 border uppercase tracking-[1px] ${showReason ? 'cursor-help ' : ''}${statusStyles[status] ?? statusStyles.draft}`}
    >
      {status}
    </span>
  );
}
