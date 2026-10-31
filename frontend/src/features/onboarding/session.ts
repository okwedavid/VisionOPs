export const COMPLETE_KEY = 'visionops.onboarding.complete'
export const PROGRESS_KEY = 'visionops.onboarding.progress'

export function restartOnboarding() {
  try {
    sessionStorage.removeItem(COMPLETE_KEY)
    sessionStorage.removeItem(PROGRESS_KEY)
  } catch { /* no-op */ }
  window.location.assign('/')
}
