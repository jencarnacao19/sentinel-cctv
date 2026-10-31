import { describe, expect, it } from 'vitest'

import { arrangeCameras } from '@/features/live/useWall'

const cameras = [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'new' }]

describe('arrangeCameras', () => {
  it('keeps every camera in its usual order before the wall is arranged', () => {
    expect(arrangeCameras(cameras)).toEqual(cameras)
  })

  it("follows the administrator's order, hides what was hidden and puts new cameras last", () => {
    const arranged = arrangeCameras(cameras, {
      columns: 'auto',
      order: ['c', 'a', 'b'],
      hidden: ['b'],
    })
    expect(arranged.map((camera) => camera.id)).toEqual(['c', 'a', 'new'])
  })
})
