import type {
  TemplateAnalysisReview,
  TemplateCorrectionRequest,
} from '../api/types';

/** Build the browser payload without granting protected units an override path. */
export function buildTemplateCorrectionRequest(
  review: TemplateAnalysisReview,
  selectedSuggestionIds: Set<string>,
  lockedUnitIds: Set<string>,
  comment: string,
): TemplateCorrectionRequest {
  const suggestions = review.suggestions.filter((suggestion) => (
    selectedSuggestionIds.has(suggestion.semantic_unit_id)
    && !suggestion.target_unit_ids.some((unitId) => lockedUnitIds.has(unitId))
  ));

  return {
    expected_content_hash: review.content_hash,
    selected_suggestion_ids: suggestions.map((suggestion) => suggestion.semantic_unit_id),
    locked_unit_ids: [...lockedUnitIds].sort(),
    comment,
  };
}
