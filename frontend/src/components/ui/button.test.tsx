import { render, screen } from '@testing-library/react'
import { Plus } from 'lucide-react'
import { describe, expect, it } from 'vitest'

import { Button } from '@/components/ui/button'

describe('Button', () => {
  it('slots a compound link as one child', () => {
    render(
      <Button asChild>
        <a href="/cameras">
          <Plus aria-hidden="true" />
          Add camera
        </a>
      </Button>,
    )

    const link = screen.getByRole('link', { name: 'Add camera' })
    expect(link).toHaveAttribute('href', '/cameras')
    expect(link).toHaveClass('inline-flex')
  })
})
