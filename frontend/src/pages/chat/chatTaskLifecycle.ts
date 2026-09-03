export type ChatStreamDetachReason =
  | 'component_unmount'
  | 'route_navigation'
  | 'knowledge_base_switch'
  | 'user_stop';

/** Only an explicit Stop action is allowed to mutate the server turn state. */
export function shouldCancelServerTurn(reason: ChatStreamDetachReason): boolean {
  return reason === 'user_stop';
}
