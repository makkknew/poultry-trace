import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Login } from './App'

describe('login screen', () => {
  it('submits the entered credentials to the server handler', async () => {
    const onLogin = vi.fn().mockResolvedValue(undefined)
    render(<Login onLogin={onLogin} />)
    fireEvent.change(screen.getByPlaceholderText('Например, inc_op1'), { target: { value: 'inc_op1' } })
    fireEvent.change(screen.getByPlaceholderText('Введите пароль'), { target: { value: 'Demo-Change-2026!' } })
    fireEvent.click(screen.getByRole('button', { name: /Войти/ }))
    await waitFor(() => expect(onLogin).toHaveBeenCalledWith('inc_op1', 'Demo-Change-2026!'))
  })

  it('shows a useful server error and lets the user reveal the password', async () => {
    const onLogin = vi.fn().mockRejectedValue(new Error('Неверный логин или пароль'))
    render(<Login onLogin={onLogin} />)
    const password = screen.getByPlaceholderText('Введите пароль') as HTMLInputElement
    fireEvent.change(password, { target: { value: 'Wrong-password-2026' } })
    fireEvent.click(screen.getByRole('button', { name: 'Показать' }))
    expect(password.type).toBe('text')
    fireEvent.click(screen.getByRole('button', { name: /Войти/ }))
    expect(await screen.findByText('Неверный логин или пароль')).toBeInTheDocument()
  })
})
