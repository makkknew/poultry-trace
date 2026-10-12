export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message)
  }
}

export const forbidden = () => new ApiError(403, 'FORBIDDEN', 'Недостаточно прав для этого действия')
export const notFound = () => new ApiError(404, 'NOT_FOUND', 'Запись не найдена')
export const conflict = (code: string, message: string) => new ApiError(409, code, message)
